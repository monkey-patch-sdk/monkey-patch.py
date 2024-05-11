import abc 
from pydantic import BaseModel#, ConfigDict
from typing import Optional
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

class BaseModelConfig(abc.ABC, BaseModel):
    """
    The abstract Basemodel class for all model configs.

    Parameters
    ----------
    model_name : str -- the name of the model
    provider : str -- the name of the provider (api provider)
    context_length : int -- the context length of the model
    chat_template : Optional[str] -- the chat template for the model
    system_message : Optional[str] -- the system message for the model
    instructions : Optional[str] -- the instructions for the model
    parsing_helper_tokens : Optional[dict] -- the parsing helper tokens for the model
    """
    #model_config = ConfigDict(
    #        protected_namespaces=()
    #    )
    model_name: str
    provider: str
    context_length: int
    chat_template : Optional[str] = None
    system_message : str = f"You are a skillful and accurate language model, who applies a described function on input data. Make sure the function is applied accurately and correctly and the outputs follow the output type hints and are valid outputs given the output types."
    instructions : str = "You are given below a function description and input data. The function description of what the function must carry out can be found in the Function section, with input and output type hints. The input data can be found in Input section. Using the function description, apply the function to the Input and return a valid output type, that is acceptable by the output_class_definition and output_class_hint. Return None if you can't apply the function to the input or if the output is optional and the correct output is None.\nINCREDIBLY IMPORTANT: Only output a JSON-compatible string in the correct response format."
    repair_instruction: str = "Below are an outputs of a function applied to inputs, which failed type validation. The input to the function is brought out in the INPUT section and function description is brought out in the FUNCTION DESCRIPTION section. Your task is to apply the function to the input and return a correct output in the right type. The FAILED EXAMPLES section will show previous outputs of this function applied to the data, which failed type validation and hence are wrong outputs. Using the input and function description output the accurate output following the output_class_definition and output_type_hint attributes of the function description, which define the output type. Make sure the output is an accurate function output and in the correct type. Return None if you can't apply the function to the input or if the output is optional and the correct output is None."
    system_message_token_count: int = -1
    instruction_token_count: int = -1
    parsing_helper_tokens: Optional[dict] = {"start_token": "", "end_token": ""}
    base_model_for_sft: str = ""