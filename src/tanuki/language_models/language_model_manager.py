import json
from typing import Any, Dict

from tanuki.function_modeler import FunctionModeler
from tanuki.language_models.llm_api_abc import LLM_API
from tanuki.models.function_description import FunctionDescription
from tanuki.models.function_example import FunctionExample
from tanuki.models.language_model_output import LanguageModelOutput
from tanuki.utils import approximate_token_count, get_string_represntation, prepare_object_for_saving
from tanuki.validator import Validator


class LanguageModelManager(object):
    """
    The LanguageModelManager is responsible for managing the language models and their outputs operationally,
    this includes:
    - Generating outputs from the language models
    - Repairing outputs from the language models
    - Saving outputs from the language models
    - Finetuning the language models from the saved outputs
    """

    def __init__(self,
                 function_modeler: FunctionModeler,
                 generation_token_limit=512,
                 api_providers: Dict[str, LLM_API] = None) -> None:
        self.api_providers = api_providers
        self.function_modeler = function_modeler
        self.instruction = "You are given below a function description and input data. The function description of what the function must carry out can be found in the Function section, with input and output type hints. The input data can be found in Input section. Using the function description, apply the function to the Input and return a valid output type, that is acceptable by the output_class_definition and output_class_hint. Return None if you can't apply the function to the input or if the output is optional and the correct output is None.\nINCREDIBLY IMPORTANT: Only output a JSON-compatible string in the correct response format."
        self.system_message = f"You are a skillful and accurate language model, who applies a described function on input data. Make sure the function is applied accurately and correctly and the outputs follow the output type hints and are valid outputs given the output types."

        self.instruction_token_count = approximate_token_count(self.instruction)
        self.system_message_token_count = approximate_token_count(self.system_message)
        self.repair_instruction = "Below are an outputs of a function applied to inputs, which failed type validation. The input to the function is brought out in the INPUT section and function description is brought out in the FUNCTION DESCRIPTION section. Your task is to apply the function to the input and return a correct output in the right type. The FAILED EXAMPLES section will show previous outputs of this function applied to the data, which failed type validation and hence are wrong outputs. Using the input and function description output the accurate output following the output_class_definition and output_type_hint attributes of the function description, which define the output type. Make sure the output is an accurate function output and in the correct type. Return None if you can't apply the function to the input or if the output is optional and the correct output is None."
        self.default_generation_length = generation_token_limit

    def __call__(self,
                 args,
                 function_description: FunctionDescription,
                 kwargs,
                 validator: Validator,
                 generation_parameters: dict) -> Any:
        
        # add the generation length if not there
        if "max_new_tokens" not in generation_parameters:
            generation_parameters["max_new_tokens"] = self.default_generation_length

        output = self.generate(args, kwargs, function_description, generation_parameters)
        # start parsing the object, very hacky way for the time being
        choice_parsed = self._parse_choice(output)
        valid = validator.check_type(choice_parsed, function_description.output_type_hint)
        if not valid:
            choice, choice_parsed, successful_repair = self.repair_output(args,
                                                                          kwargs,
                                                                          function_description,
                                                                          output.generated_response,
                                                                          validator)

            if not successful_repair:
                raise TypeError(
                    f"Output type was not valid. Expected an object of type {function_description.output_type_hint}, got '{output.generated_response}'")
            output.generated_response = choice
            output.distilled_model = False
        datapoint = FunctionExample(args, kwargs, output.generated_response)
        if output.suitable_for_finetuning and not output.distilled_model:
            self.function_modeler.postprocess_symbolic_datapoint(function_description.__hash__(), function_description,
                                                                 datapoint, repaired=not valid)
        instantiated = validator.instantiate(choice_parsed, function_description.output_type_hint)
        return instantiated

    def _parse_choice(self, output):
        if isinstance(output.generated_response, dict):
            return output.generated_response
        try:
            # json load
            choice_parsed = json.loads(output.generated_response)
        except:
            # if it fails, it's not a json object, try eval
            try:
                choice_parsed = eval(output.generated_response)
            except:
                choice_parsed = output.generated_response
        return choice_parsed

    def generate(self, args, kwargs, function_description, llm_parameters={}):
        """
        The main generation function, given the args, kwargs, function description and model type, generate a response and check if the datapoint can be saved to the finetune dataset
        """

        prompt, model, save_to_finetune, is_distilled_model = self.get_generation_case(args, kwargs,
                                                                                       function_description,
                                                                                       llm_parameters)
        choice = self._synthesise_answer(prompt, model, llm_parameters)

        output = LanguageModelOutput(choice, save_to_finetune, is_distilled_model)
        return output

    def _synthesise_answer(self, prompt, model, llm_parameters):
        """
        Synthesise an answer given the prompt, model, model_type and llm_parameters
        """
        system_message = model.system_message if model.system_message else self.system_message
        if model.provider not in self.api_providers:
            raise ValueError(f"Model provider {model.provider} not found in api_providers."\
                              "If you have integrated a new provider, please add it to the api_providers dict in the LanguageModelManager constructor"\
                              "and create a relevant API class to carry out the synthesis")
        return self.api_providers[model.provider].generate(model, system_message, prompt, **llm_parameters)


    def get_generation_case(self, args, kwargs, function_description, llm_parameters):
        """
        Get the generation case with the correct prompt and model
        First get the current model, then if distilled model, do zero-shot prompt and return False as suitable_for_finetune
        If not distilled model, check if suitable for finetuning, create the prompt and return the correct model given the token count
        """
        f = str(function_description.__dict__.__repr__())

        distilled_model, teacher_models = self.function_modeler.get_models(function_description)
        is_distilled_model = distilled_model.model_name != ""
        suitable_for_distillation, input_prompt_token_count = self.suitable_for_finetuning_token_check(args, kwargs, f,
                                                                                                       distilled_model.context_length)
        # no examples needed, using a finetuned model. Dont save to finetune dataset
        if is_distilled_model and suitable_for_distillation:
            prompt = self.construct_prompt(f, args, kwargs, [], distilled_model)
            return prompt, distilled_model, suitable_for_distillation, True

        else:
            aligns = self.function_modeler.get_symbolic_alignments(function_description.__hash__(), max=16)
            examples = [f"Inputs:\nArgs: {align['args']}\nKwargs: {align['kwargs']}\nOutput: {align['output']}" for align in
                 aligns]
            examples = self.create_examples(aligns)
            examples_token_count = sum([approximate_token_count(example) for example in examples])
            generation_tokens = llm_parameters.get("max_new_tokens", self.default_generation_length)
            model = self.choose_model_from_tokens(teacher_models,
                                                  examples_token_count + input_prompt_token_count + generation_tokens,
                                                  len(examples))
            prompt = self.construct_prompt(f, args, kwargs, examples, model)
            if model:
                return prompt, model, suitable_for_distillation, False
            else:
                raise ValueError(
                    "The input content and align statements combined are too long, please shorten it. The maximum currently allowed token limit is 32000")

    def create_examples(self, aligns):
        """
        Create examples given the aligns
        """
        final_examples = []
        for align in aligns:
            # create a json dumped examples
            args = get_string_represntation(align["args"])
            kwargs = get_string_represntation(align["kwargs"])
            output = get_string_represntation(align["output"])
            final_examples.append(f"Inputs:\nArgs: {args}\nKwargs: {kwargs}\nOutput: {output}")
        #examples = [f"Inputs:\nArgs: {align['args']}\nKwargs: {align['kwargs']}\nOutput: {align['output']}" for align in
        #         aligns]
        return final_examples
    def suitable_for_finetuning_token_check(self, args, kwargs, f, distillation_token_count):
        """
        Check if the inputs are suitable for finetuning, i.e are below the finetuning token count
        """
        # check if finetunable
        finetuning_prompt = f"Function: {f}\n---\nInputs:\nArgs: {args}\nKwargs: {kwargs}\nOutput:"
        input_prompt_token_count = approximate_token_count(finetuning_prompt)
        suitable_for_finetune = input_prompt_token_count + self.instruction_token_count + self.system_message_token_count < distillation_token_count
        return suitable_for_finetune, input_prompt_token_count

    def construct_prompt(self, f, args, kwargs, examples, model):
        """
        Cosntruct a prompt given the function description, args, kwargs and examples
        """
        if examples:
            final_examples = "\n".join(
                    [f"{model.parsing_helper_tokens['start_token']}{align}{model.parsing_helper_tokens['end_token']}" for align in
                     examples])
            example_input = f"Examples:{final_examples}\n"
        else:
            example_input = ""

        instruction_prompt = model.instructions if model.instructions else self.instruction
        # make args and kwargs same representation as loaded examples
        args = get_string_represntation(prepare_object_for_saving(args))
        kwargs = get_string_represntation(prepare_object_for_saving(kwargs))
        content = f"{instruction_prompt}\nFunction: {f}\n{example_input}---\n{model.parsing_helper_tokens['start_token']}Inputs:\nArgs: {args}\nKwargs: {kwargs}\nOutput:"
        return content

    def repair_generate(self, args, kwargs, f, failed_outputs_list, examples, models):
        """
        Repair the output given the input, function description, failed outputs list, examples and models
        """
        prompt = self.generate_repair_prompt(args, kwargs, f, failed_outputs_list, examples)
        prompt_token_count = approximate_token_count(prompt)
        model = self.choose_model_from_tokens(models, prompt_token_count)
        if model:
            choice = self._synthesise_answer(prompt, model, {})
            return choice
        else:
            return None

    def generate_repair_prompt(self, args, kwargs, f, failed_outputs_list, examples):
        """
        Generate a repair prompt given the args, kwargs, function description, failed outputs list and examples
        """

        failed_examples = ""
        for failed_output in failed_outputs_list:
            failed_examples += f"Output: {failed_output[0]}\nError: {failed_output[1]}\n\n"
        successful_examples = f"Successful Examples:{examples}\n" if examples else ""
        prompt = f"{self.repair_instruction}\nFUNCTION DESCRIPTION: {f}\n{successful_examples}---Inputs:\nArgs: {args}\nKwargs: {kwargs}\nFAILED EXAMPLES: {failed_examples}Correct output:"
        return prompt

    def choose_model_from_tokens(self, models, input_token_count, nr_of_examples=0):
        """
        Choose a model from the models given the token count
        """

        for model in models:
            # check if input token count is less than the context length
            # If the model config has custom messages, then use those, otherwise use the default ones
            if model.system_message is not None:
                system_message_token_count = approximate_token_count(model.system_message)
            else:
                system_message_token_count = self.system_message_token_count
            if model.instructions is not None:
                instructions_token_count = approximate_token_count(model.instructions)
            else:
                instructions_token_count = self.instruction_token_count
            if model.parsing_helper_tokens["start_token"]:
                input_token_count += 2*nr_of_examples
            if model.parsing_helper_tokens["end_token"]:
                input_token_count += 2*nr_of_examples
            total_token_count = input_token_count + instructions_token_count + system_message_token_count
            if total_token_count < model.context_length:
                return model
        return None

    def repair_output(self,
                      args: tuple,
                      kwargs: dict,
                      function_description: FunctionDescription,
                      choice,
                      validator: Validator) -> tuple:
        """
        Repair an output, that failed type validation by generating a new output using the teacher model and the error
        Args:
            args (tuple): The args of the function
            kwargs (dict): The kwargs of the function
            function_description (FunctionDescription): The function description
            choice: The output that failed type validation, type is arbitrary
            validator (Validator): The validator object

        Returns:
            choice (str): The choice that was generated by the language model
            choice_parsed: The parsed choice, type is arbitrary
            valid (bool): Whether the output was correctly repaired was valid
        """

        # get the teacher models
        teacher_models = self.function_modeler.get_models(function_description)[1]
        valid = False
        retry_index = 5
        f = str(function_description.__dict__.__repr__() + "\n")
        error = f"Output type was not valid. Expected an valid object of type {function_description.output_type_hint}, got '{choice}'"
        # instantiate the failed outputs list
        failed_outputs_list = [(choice, error)]
        while retry_index > 0 and not valid:
            # get the alignments
            aligns = self.function_modeler.get_symbolic_alignments(function_description.__hash__(), max=5)
            examples = "\n".join(
                [f"Inputs:\nArgs: {align['args']}\nKwargs {align['kwargs']}\nOutput: {align['output']}" for align in
                 aligns])
            # Generate the reparied LLM output
            choice = self.repair_generate(args, kwargs, f, failed_outputs_list, examples, teacher_models)
            if not choice:
                # if no choice then the input was too long for the model
                # no specific error but the retry index goes down
                retry_index -= 1
                continue

            # start parsing the object
            try:
                # json load
                choice_parsed = json.loads(choice)
            except:
                # if it fails, it's not a json object, try eval
                try:
                    choice_parsed = eval(choice)
                except:
                    choice_parsed = choice

            valid = validator.check_type(choice_parsed, function_description.output_type_hint)
            if not valid:
                # if it's not valid, add it to the failed outputs list
                error = f"Output type was not valid. Expected an object of type {function_description.output_type_hint}, got '{choice}'"
                failed_outputs_list.append((choice, error))
                retry_index -= 1

        return choice, choice_parsed, valid
