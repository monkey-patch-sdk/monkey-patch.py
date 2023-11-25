from typing import Literal

from monkey_patch.bloom_filter import BloomFilter
from monkey_patch.persistence.filter.bloom_filtered_persistence import BloomFilteredPersistence
from monkey_patch.persistence.persistence_layer import FileSystemPersistence, RedisPersistence, S3Persistence
from monkey_patch.persistence.persistence_layer_interface import IPersistenceLayer

PersistenceType = Literal["filesystem", "redis", "s3"]

class PersistenceFactory:
    @staticmethod
    def create_persistence(type: PersistenceType, bloom_filter: BloomFilter) -> IPersistenceLayer:
        if type == "filesystem":
            return BloomFilteredPersistence(FileSystemPersistence(), bloom_filter)
        elif type == "redis":
            return BloomFilteredPersistence(RedisPersistence(), bloom_filter)
        elif type == "s3":
            return BloomFilteredPersistence(S3Persistence(), bloom_filter)
        else:
            raise ValueError("Unknown persistence type")
