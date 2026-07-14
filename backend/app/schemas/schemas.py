import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models import AssetKind, BackendKind, ExecutionType, JobStatusEnum, NodeKind, NodeStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Backend ----------
class BackendCreate(BaseModel):
    name: str
    kind: BackendKind
    base_url: str | None = None
    is_active: bool = True


class BackendUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    is_active: bool | None = None


class BackendRead(ORMModel):
    id: uuid.UUID
    name: str
    kind: BackendKind
    base_url: str | None
    is_active: bool
    last_heartbeat_at: datetime | None
    last_stats: dict
    created_at: datetime


# ---------- Capability ----------
class CapabilityCreate(BaseModel):
    backend_id: uuid.UUID
    node_type_slug: str
    enabled: bool = True
    execution_type: ExecutionType
    config: dict[str, Any] = {}


class CapabilityUpdate(BaseModel):
    node_type_slug: str | None = None
    enabled: bool | None = None
    execution_type: ExecutionType | None = None
    config: dict[str, Any] | None = None


class CapabilityRead(ORMModel):
    id: uuid.UUID
    backend_id: uuid.UUID
    node_type_slug: str
    enabled: bool
    execution_type: ExecutionType
    config: dict
    created_at: datetime


# ---------- NodeTemplate ----------
class NodeTemplateCreate(BaseModel):
    node_type_slug: str
    name: str
    param_schema: dict[str, Any] = {}
    defaults: dict[str, Any] = {}


class NodeTemplateUpdate(BaseModel):
    node_type_slug: str | None = None
    name: str | None = None
    param_schema: dict[str, Any] | None = None
    defaults: dict[str, Any] | None = None


class NodeTemplateRead(ORMModel):
    id: uuid.UUID
    node_type_slug: str
    name: str
    param_schema: dict
    defaults: dict
    created_at: datetime


# ---------- Workflow analysis (template creation wizard) ----------
class WorkflowNodeInfoOut(BaseModel):
    node_id: str
    class_type: str
    title: str | None


class DetectedFieldOut(BaseModel):
    key: str
    label: str
    type: str
    node_id: str
    input_key: str
    default: Any = None


class WorkflowAnalysisOut(BaseModel):
    input_image_nodes: list[WorkflowNodeInfoOut]
    output_nodes: list[WorkflowNodeInfoOut]
    detected_fields: list[DetectedFieldOut]
    duplicate_titles: list[str]


# ---------- Project ----------
class ProjectCreate(BaseModel):
    name: str


class ProjectRead(ORMModel):
    id: uuid.UUID
    name: str
    start_kind: NodeKind | None
    created_at: datetime


# ---------- Track ----------
class TrackCreate(BaseModel):
    project_id: uuid.UUID
    row_index: int
    spawned_from_node_id: uuid.UUID | None = None
    spawned_from_output_id: uuid.UUID | None = None


class TrackUpdate(BaseModel):
    row_index: int


class TrackRead(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    row_index: int
    spawned_from_node_id: uuid.UUID | None
    spawned_from_output_id: uuid.UUID | None
    created_at: datetime


# ---------- InputRef (tagged union, stored as plain dict in JSONB) ----------
class InputRef(BaseModel):
    type: str  # self_prev | track_below_prev | explicit | upload | text
    node_id: uuid.UUID | None = None
    output_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    value: str | None = None


# ---------- Node ----------
class NodeCreate(BaseModel):
    track_id: uuid.UUID
    step_index: int
    kind: NodeKind = NodeKind.workflow
    template_id: uuid.UUID | None = None
    inputs: list[InputRef] = []
    params: dict[str, Any] = {}
    requested_variants: int = 1
    backend_mode: str = "auto"
    manual_backend_id: uuid.UUID | None = None


class NodeUpdate(BaseModel):
    template_id: uuid.UUID | None = None
    inputs: list[InputRef] | None = None
    params: dict[str, Any] | None = None
    requested_variants: int | None = None
    status: NodeStatus | None = None
    backend_mode: str | None = None
    manual_backend_id: uuid.UUID | None = None
    is_picker: bool | None = None


class NodeRead(ORMModel):
    id: uuid.UUID
    track_id: uuid.UUID
    step_index: int
    kind: NodeKind
    is_picker: bool
    template_id: uuid.UUID | None
    inputs: list[dict]
    params: dict
    status: NodeStatus
    backend_used_id: uuid.UUID | None
    requested_variants: int
    backend_mode: str
    manual_backend_id: uuid.UUID | None
    error: str | None
    created_at: datetime


# ---------- Asset ----------
class AssetRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID | None
    storage_key: str
    mime_type: str
    kind: AssetKind
    selected: bool
    meta: dict
    created_at: datetime
    url: str | None = None


class AssetSelectUpdate(BaseModel):
    selected: bool


class AssetMoveUpdate(BaseModel):
    node_id: uuid.UUID


# ---------- Job ----------
class JobRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID
    backend_id: uuid.UUID | None
    variant_index: int
    status: JobStatusEnum
    external_job_id: str | None
    retries: int
    error: str | None
    progress: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ---------- ApiKeyPermission ----------
class ApiKeyPermissionCreate(BaseModel):
    provider: str
    node_type_slug: str
    api_key: str
    enabled: bool = True


class ApiKeyPermissionRead(ORMModel):
    id: uuid.UUID
    provider: str
    node_type_slug: str
    enabled: bool
    created_at: datetime
