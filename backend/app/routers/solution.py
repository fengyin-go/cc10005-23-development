"""采集方案发行包接口：制作端冻结导出、接收端预览决策与启用的完整工作区。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.solution import OBJECT_TYPES, SolutionService

router = APIRouter(prefix="/api/solution", tags=["采集方案发行包"])

service = SolutionService()


class CreatePackagePayload(BaseModel):
    name: str
    description: str | None = None


class SelectionPayload(BaseModel):
    selection: list[dict[str, Any]] = Field(default_factory=list)


class UploadPayload(BaseModel):
    file: dict[str, Any]


class DecisionPayload(BaseModel):
    fingerprint: str
    decision: str


class CredentialPayload(BaseModel):
    credential_ref: str


class JobBindPayload(BaseModel):
    job_id: int


def _fail(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


# ---------------------------------------------------------------- 制作端接口


@router.get("/packages")
def list_packages(status: str | None = None) -> dict[str, Any]:
    """方案包工作区列表：按制作状态过滤。"""
    items = service.list_packages(status=status)
    return {"items": items, "total": len(items)}


@router.get("/packages/{package_id}")
def get_package(package_id: int) -> dict[str, Any]:
    package = service.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"方案包 {package_id} 不存在")
    return package


@router.get("/sources/{obj_type}")
def list_sources(obj_type: str) -> dict[str, Any]:
    """制作端挑选对象：返回站点/抽取模板/字段映射/调度策略及其确切版本指纹。"""
    if obj_type not in OBJECT_TYPES:
        raise _fail(f"不支持的对象类型：{obj_type}，可选 { '、'.join(OBJECT_TYPES) }")
    return service.list_source_objects(obj_type)


@router.post("/packages")
def create_package(payload: CreatePackagePayload) -> dict[str, Any]:
    package, message = service.create_package({"name": payload.name, "description": payload.description})
    if package is None:
        raise _fail(message)
    return {"ok": True, "message": "方案包工作区已创建", "entry": package}


@router.put("/packages/{package_id}/selection")
def update_selection(package_id: int, payload: SelectionPayload) -> dict[str, Any]:
    package, message = service.update_selection(package_id, payload.selection)
    if package is None:
        raise _fail(message)
    return {"ok": True, "message": "挑选范围已更新", "entry": package}


@router.post("/packages/{package_id}/freeze")
def freeze_package(package_id: int) -> dict[str, Any]:
    """冻结各对象的确切版本，生成独立身份与内容摘要。"""
    package, message = service.freeze(package_id)
    if package is None:
        raise _fail(message)
    return {"ok": True, "message": "方案已冻结，对象版本与引用关系已固定", "entry": package}


@router.post("/packages/{package_id}/export")
def export_package(package_id: int) -> dict[str, Any]:
    """导出可下载的方案文件（不含认证秘密）。"""
    file_payload, message = service.export_file(package_id)
    if file_payload is None:
        raise _fail(message)
    return {"ok": True, "message": "方案文件已生成，可下载分发", "file": file_payload}


# ---------------------------------------------------------------- 接收端接口


@router.get("/receipts")
def list_receipts() -> dict[str, Any]:
    items = service.list_receipts()
    return {"items": items, "total": len(items)}


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int) -> dict[str, Any]:
    receipt = service.get_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"接收记录 {receipt_id} 不存在")
    return receipt


@router.post("/verify")
def verify_file(payload: UploadPayload) -> dict[str, Any]:
    """只做文件完整性验证并返回差异预览，不产生接收记录。"""
    valid, message = service.verify_integrity(payload.file)
    if not valid:
        return {"ok": False, "message": message}
    diff_tree = service.preview_diff(payload.file)
    return {"ok": True, "message": message, "diff_tree": diff_tree}


@router.post("/receipts")
def upload_package(payload: UploadPayload) -> dict[str, Any]:
    """上传方案文件：完整性验证通过后展开引用差异树，等待逐项决策。"""
    receipt, message = service.upload(payload.file)
    if receipt is None:
        raise _fail(message)
    return {"ok": True, "message": message or "方案已接收，请在差异树中逐项决策", "entry": receipt}


@router.post("/receipts/{receipt_id}/decisions")
def decide_node(receipt_id: int, payload: DecisionPayload) -> dict[str, Any]:
    """对同名对象逐项决定复用还是另建。"""
    receipt, message = service.decide_node(receipt_id, payload.fingerprint, payload.decision)
    if receipt is None:
        raise _fail(message)
    return {"ok": True, "message": f"已记录决策：{payload.decision}", "entry": receipt}


@router.post("/receipts/{receipt_id}/credentials")
def bind_credential(receipt_id: int, payload: CredentialPayload) -> dict[str, Any]:
    """目标环境另行绑定凭证；秘密不随包转移。"""
    receipt, message = service.bind_credential(receipt_id, {"credential_ref": payload.credential_ref})
    if receipt is None:
        raise _fail(message)
    return {"ok": True, "message": "凭证已在目标环境绑定，方案具备执行资格", "entry": receipt}


@router.post("/receipts/{receipt_id}/activate")
def activate_receipt(receipt_id: int) -> dict[str, Any]:
    """启用方案：未决引用或未绑定凭证都会阻止；绝不边导入边替换现用配置。"""
    receipt, message = service.activate(receipt_id)
    if receipt is None:
        raise _fail(message)
    return {"ok": True, "message": message or "方案已启用，只供之后建立的采集作业使用", "entry": receipt}


@router.post("/receipts/{receipt_id}/reject")
def reject_receipt(receipt_id: int) -> dict[str, Any]:
    receipt, message = service.reject(receipt_id)
    if receipt is None:
        raise _fail(message)
    return {"ok": True, "message": "接收记录已拒绝", "entry": receipt}


@router.post("/receipts/{receipt_id}/jobs")
def bind_job(receipt_id: int, payload: JobBindPayload) -> dict[str, Any]:
    """把启用后的方案绑定到新采集作业；在途作业会被拒绝并保留原方案证据。"""
    job, message = service.bind_job(payload.job_id, receipt_id)
    if job is None:
        raise _fail(message)
    return {"ok": True, "message": "采集作业已绑定该方案", "entry": job}


@router.get("/job-evidence")
def job_evidence() -> dict[str, Any]:
    """在途作业保留的原方案证据清单。"""
    items = service.list_job_evidence()
    return {"items": items, "total": len(items)}


@router.get("/credentials")
def list_credentials() -> dict[str, Any]:
    """目标环境可绑定的凭证清单（不含秘密内容，只给编号与状态）。"""
    items = service.list_credentials()
    return {"items": items, "total": len(items)}
