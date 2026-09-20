from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


ScalarType = Literal[
    "string", "integer", "number", "boolean", "date", "datetime", "array", "object"
]


class TargetField(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    data_type: ScalarType
    required: bool = False
    unique: bool = False
    aliases: list[str] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)
    example_values: list[Any] = Field(default_factory=list)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        return value or []


class EntityRelationship(BaseModel):
    field: str
    references_entity: str
    references_field: str


class EntitySchema(BaseModel):
    entity_name: str = Field(min_length=1)
    description: str = ""
    fields: list[TargetField] = Field(min_length=1)
    relationships: list[EntityRelationship] = Field(default_factory=list)
    source_precedence: list[str] = Field(default_factory=list)


class TargetSchema(BaseModel):
    version: str = "1.0"
    entities: list[EntitySchema] = Field(min_length=1)

    @field_validator("entities")
    @classmethod
    def unique_entity_names(cls, value: list[EntitySchema]) -> list[EntitySchema]:
        names = [item.entity_name.strip().lower() for item in value]
        if len(names) != len(set(names)):
            raise ValueError("Entity names must be unique")
        return value
