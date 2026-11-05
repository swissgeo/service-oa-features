from enum import StrEnum

from pydantic import BaseModel, Field


class SyntheticzStatus(StrEnum):
    """Status of the service or of one of its external systems."""

    UP = "UP"
    DOWN = "DOWN"


class SyntheticzServiceDetails(BaseModel):
    name: str = Field(
        description="Name of the service",
        examples=["service-oa-features"],
    )
    version: str = Field(
        description="Version of the service",
        examples=["v0.1.0"],
    )


class SyntheticzExternalSystem(BaseModel):
    status: SyntheticzStatus = Field(
        description="UP when the external system is reachable and healthy, DOWN otherwise",
        examples=[SyntheticzStatus.UP],
    )


class Syntheticz(BaseModel):
    service: SyntheticzServiceDetails = Field(description="Identification of the checked service")
    status: SyntheticzStatus = Field(
        description="UP only when every external system is UP, DOWN otherwise",
        examples=[SyntheticzStatus.UP],
    )
    external_systems: dict[str, SyntheticzExternalSystem] = Field(
        description="Status of each external system the service depends on, keyed by system name",
        examples=[{"postgresql": {"status": SyntheticzStatus.UP}}],
    )
