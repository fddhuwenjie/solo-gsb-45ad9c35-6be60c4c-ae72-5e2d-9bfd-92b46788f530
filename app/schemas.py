"""请求/响应模型。"""
from pydantic import BaseModel, Field


class CellCreate(BaseModel):
    cell_id: str = Field(min_length=1, max_length=64)
    chemistry: str = Field(min_length=1, max_length=64, description="化学体系，如 LFP/NCM")
    capacity: float = Field(gt=0, description="容量 Ah")
    resistance: float = Field(gt=0, description="内阻 mΩ")
    test_version: str = Field(default="v1", min_length=1, max_length=64)


class TestRevision(BaseModel):
    """复测修订：更新检测结果并提升检测版本。"""

    capacity: float = Field(gt=0)
    resistance: float = Field(gt=0)
    test_version: str = Field(min_length=1, max_length=64)


class IsolateRequest(BaseModel):
    """安全隔离：提交隔离原因，记录一次不可撤销的隔离（复测不解除）。"""

    reason: str = Field(min_length=1, max_length=500, description="隔离原因")


class PreselectRequest(BaseModel):
    series_count: int = Field(ge=3, le=6, description="串联节数 3~6")
    min_capacity: float = Field(gt=0, description="最低单芯容量 Ah")
    max_capacity_diff: float = Field(ge=0, description="组内容量差上限 Ah")
    max_resistance_diff: float = Field(ge=0, description="组内内阻差上限 mΩ")
    chemistry: str | None = Field(default=None, description="限定化学体系；缺省则在各体系中择优")


class ConfirmCell(BaseModel):
    cell_id: str
    test_version: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    cells: list[ConfirmCell] = Field(min_length=3, max_length=6)


class BatchCreate(BaseModel):
    """交付批次登记：提交 2~4 个不重复的已确认组号。"""

    group_ids: list[int] = Field(min_length=2, max_length=4)


class CancelRequest(BaseModel):
    """撤销未发运批次：提交非空撤销原因（记录后不可改写）。"""

    reason: str = Field(min_length=1, max_length=500, description="撤销原因")
