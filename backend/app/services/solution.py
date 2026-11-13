"""采集方案发行包业务规则。

发行包工作区覆盖两条方向相反的链路：

* 制作端：从站点挑选抽取模板、字段映射、调度策略 → 冻结各对象的确切版本 →
  导出带独立身份与内容摘要的方案文件。
* 接收端：上传方案文件 → 完整性验证 → 展开引用差异树 → 对同名对象逐项决定
  复用还是另建 → 未决引用清零后才允许启用。

关键不变量（任何接口都不得破坏）：

1. 未决引用阻止整包启用；导入过程绝不替换现用配置，只允许「复用」或「另建」。
2. 包有独立身份（package_id）与内容摘要（content_digest）。同一身份同一内容
   二次接收直接返回原映射，不再复制对象；同一身份内容不同则拒绝认领。
3. 新方案启用后只供之后建立的采集作业使用，在途作业保留原方案证据。
4. 环境专属认证秘密不随包转移，目标环境另行绑定凭证后才具备执行资格。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

# 可被发行包引用的业务对象类型：(类型标识, 业务模块表名, 同名校验所用名称字段)
OBJECT_TYPES: dict[str, tuple[str, str]] = {
    "site": ("site", "站点编号"),
    "template": ("template", "模板编号"),
    "extractor": ("extractor", "规则编号"),
    "schedule": ("schedule", "规则编号"),
}

# 引用关系：方案主体类型 -> 它引用的其它对象类型
REFERENCE_GRAPH: dict[str, list[str]] = {
    "template": ["site", "extractor"],
    "schedule": ["site"],
    "site": [],
    "extractor": [],
}

PACKAGE_MODULE = "solution_package"
RECEIPT_MODULE = "solution_receipt"
CREDENTIAL_MODULE = "solution_credential"
EVIDENCE_MODULE = "solution_job_evidence"

# 方案包生命周期
PACKAGE_STATUSES = ["编辑中", "已冻结", "已导出"]
# 接收记录生命周期
RECEIPT_STATUSES = ["待决策", "待启用", "已启用", "已拒绝"]
# 差异节点决策状态
NODE_UNDECIDED = "未决"
NODE_REUSE = "复用"
NODE_CREATE = "另建"
NODE_CREATED = "已迁入"
NODE_REUSED = "已复用"

# 包文件格式版本，未来结构演进时据此识别
PACKAGE_FORMAT = "scraper-solution-package/1"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _canonical_json(payload: Any) -> str:
    """用稳定的键序序列化，保证同一内容在任何时刻算出相同摘要。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj_type: str, row: dict[str, Any]) -> str:
    """计算单个对象确切版本的指纹：剥离运行态字段后对内容做哈希。"""
    name_field = OBJECT_TYPES[obj_type][1]
    material = {
        "type": obj_type,
        "name": str(row.get(name_field, "")),
        "version": str(row.get("版本", row.get("模板版本", row.get("规则版本", "")))),
        "content": {k: v for k, v in row.items() if k not in ("id", "status", "pending", "abnormal")},
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()[:16]


def content_digest(payload: dict[str, Any]) -> str:
    """对包内全部冻结对象计算整体内容摘要。

    制作端（包记录）用 package_id 字段、导出文件用 package_uid 字段，这里两者兼容，
    保证「导出时算的摘要」和「接收时复核的摘要」口径完全一致。
    """
    body = {
        "format": payload.get("format"),
        "package_id": payload.get("package_id") or payload.get("package_uid"),
        "objects": payload.get("objects", []),
        "references": payload.get("references", []),
        "secret_policy": payload.get("secret_policy"),
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


class SolutionService:
    # ------------------------------------------------------------------ 制作端

    def list_packages(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PACKAGE_MODULE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def get_package(self, package_id: int) -> dict[str, Any] | None:
        return store.find(PACKAGE_MODULE, package_id)

    def list_source_objects(self, obj_type: str) -> dict[str, Any]:
        """供制作端挑选对象：返回某类型当前可选的业务对象及其版本指纹。"""
        if obj_type not in OBJECT_TYPES:
            return {"items": [], "message": f"不支持的对象类型：{obj_type}"}
        module, name_field = OBJECT_TYPES[obj_type]
        items = []
        for row in store.rows(module):
            items.append({
                "ref_id": int(row["id"]),
                "type": obj_type,
                "name": row.get(name_field),
                "status": row.get("status"),
                "version": str(row.get("模板版本", row.get("规则版本", "v1"))),
                "fingerprint": fingerprint(obj_type, row),
            })
        return {"items": items}

    def create_package(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        name = str(values.get("name") or "").strip()
        if not name:
            return None, "方案包名称必填"
        package = {
            "id": store.next_id(PACKAGE_MODULE),
            "name": name,
            "description": str(values.get("description") or "").strip(),
            "status": PACKAGE_STATUSES[0],
            "selection": [],       # 制作端挑选的对象引用 [{type, ref_id}]
            "frozen": [],          # 冻结后的对象快照（含指纹与版本）
            "references": [],      # 展开后的完整引用关系
            "package_uid": None,   # 独立身份，冻结时生成
            "content_digest": None,
            "exported_file": None,
            "created_at": _now(),
            "frozen_at": None,
            "exported_at": None,
        }
        store.rows(PACKAGE_MODULE).append(package)
        return package, ""

    def update_selection(self, package_id: int, selection: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        package = store.find(PACKAGE_MODULE, package_id)
        if package is None:
            return None, f"方案包 {package_id} 不存在"
        if package["status"] != PACKAGE_STATUSES[0]:
            return None, "方案已冻结，不能再调整挑选范围"
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for item in selection:
            obj_type = str(item.get("type") or "").strip()
            ref_id = int(item.get("ref_id") or 0)
            if obj_type not in OBJECT_TYPES:
                return None, f"不支持的对象类型：{obj_type}"
            module, name_field = OBJECT_TYPES[obj_type]
            if store.find(module, ref_id) is None:
                return None, f"{obj_type} 对象 {ref_id} 不存在，无法加入方案"
            key = (obj_type, ref_id)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"type": obj_type, "ref_id": ref_id})
        package["selection"] = normalized
        return package, ""

    def freeze(self, package_id: int) -> tuple[dict[str, Any] | None, str]:
        """冻结各对象的确切版本：快照内容、计算指纹，并展开完整引用关系。"""
        package = store.find(PACKAGE_MODULE, package_id)
        if package is None:
            return None, f"方案包 {package_id} 不存在"
        if package["status"] != PACKAGE_STATUSES[0]:
            return None, "方案已冻结，不能重复冻结"
        if not package["selection"]:
            return None, "尚未挑选任何对象，无可冻结内容"

        frozen: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        visited: set[tuple[str, int]] = set()

        def visit(obj_type: str, ref_id: int, parent: str | None) -> str | None:
            module, name_field = OBJECT_TYPES[obj_type]
            row = store.find(module, ref_id)
            if row is None:
                return f"{obj_type} 对象 {ref_id} 已被删除，引用无法冻结"
            key = (obj_type, ref_id)
            fp = fingerprint(obj_type, row)
            if key not in visited:
                visited.add(key)
                snapshot = {
                    "type": obj_type,
                    "source_id": ref_id,
                    "name": row.get(name_field),
                    "version": str(row.get("模板版本", row.get("规则版本", "v1"))),
                    "fingerprint": fp,
                    "content": copy.deepcopy(row),
                }
                frozen.append(snapshot)
                for child_type in REFERENCE_GRAPH[obj_type]:
                    # 种子数据没有真实外键，这里按同名字段尽力关联，关联不到不阻塞
                    child_name = self._resolve_reference_name(obj_type, child_type, row)
                    child_row = self._find_by_name(child_type, child_name) if child_name else None
                    if child_row is not None:
                        err = visit(child_type, int(child_row["id"]), f"{obj_type}:{fp}")
                        if err:
                            return err
            references.append({"parent": parent, "type": obj_type, "name": row.get(name_field), "fingerprint": fp})
            return None

        for item in package["selection"]:
            err = visit(item["type"], item["ref_id"], None)
            if err:
                return None, err

        package["frozen"] = frozen
        package["references"] = references
        package["status"] = PACKAGE_STATUSES[1]
        package["frozen_at"] = _now()
        # 独立身份：基于制作人、方案名与冻结时刻生成，导出后保持不变
        identity_material = _canonical_json({
            "name": package["name"],
            "created_at": package["created_at"],
            "frozen_at": package["frozen_at"],
        })
        package["package_uid"] = "PKG-" + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:12].upper()
        package["content_digest"] = self._digest_of(package)
        return package, ""

    def export_file(self, package_id: int) -> tuple[dict[str, Any] | None, str]:
        """导出可下载的方案文件：清单 + 冻结对象 + 内容摘要，不含任何认证秘密。"""
        package = store.find(PACKAGE_MODULE, package_id)
        if package is None:
            return None, f"方案包 {package_id} 不存在"
        if package["status"] == PACKAGE_STATUSES[0]:
            return None, "方案尚未冻结，不能导出"
        file_payload = {
            "format": PACKAGE_FORMAT,
            "package_uid": package["package_uid"],
            "name": package["name"],
            "description": package["description"],
            "frozen_at": package["frozen_at"],
            "objects": package["frozen"],
            "references": package["references"],
            # 显式声明：认证秘密不随包转移
            "secret_policy": "credentials_excluded_bind_on_target",
            "content_digest": package["content_digest"],
        }
        package["status"] = PACKAGE_STATUSES[2]
        package["exported_at"] = _now()
        package["exported_file"] = file_payload
        return file_payload, ""

    # ------------------------------------------------------------------ 接收端

    def list_receipts(self) -> list[dict[str, Any]]:
        return sorted(store.rows(RECEIPT_MODULE), key=lambda row: int(row.get("id", 0)), reverse=True)

    def get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        return store.find(RECEIPT_MODULE, receipt_id)

    def upload(self, file_payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上传方案文件：完整性验证 → 身份认领 → 展开引用差异树（只预览不落库）。"""
        valid, message = self.verify_integrity(file_payload)
        if not valid:
            return None, message

        uid = str(file_payload.get("package_uid") or "")
        digest = str(file_payload.get("content_digest") or "")

        # 身份认领规则：同 UID 已接收过
        existing = self._find_receipt_by_uid(uid)
        if existing is not None:
            if existing["content_digest"] == digest:
                # 同一身份同一内容：返回原映射，不再复制对象
                existing["re_uploaded"] = True
                return existing, "该方案包已接收过，直接返回原有映射，未重复复制对象"
            # 同一身份内容不同：拒绝认领
            return None, (
                f"身份冲突：包身份 {uid} 已被现有接收记录认领，但内容摘要不一致"
                f"（现有 {existing['content_digest'][:12]} / 上传 {digest[:12]}），拒绝认领"
            )

        diff_tree = self._build_diff_tree(file_payload)
        unresolved = sum(1 for node in diff_tree if node["decision"] == NODE_UNDECIDED)
        # 留存冻结对象，供启用落地阶段复制；不写入任何业务表，不触碰现用配置
        frozen_objects = [
            {"type": obj["type"], "fingerprint": obj["fingerprint"], "content": obj.get("content")}
            for obj in file_payload.get("objects", [])
        ]
        receipt = {
            "id": store.next_id(RECEIPT_MODULE),
            "package_uid": uid,
            "name": file_payload.get("name"),
            "source_frozen_at": file_payload.get("frozen_at"),
            "content_digest": digest,
            "status": RECEIPT_STATUSES[0] if unresolved else RECEIPT_STATUSES[1],
            "diff_tree": diff_tree,
            "object_mapping": [],       # 启用/迁入后建立的 源指纹 -> 目标对象 映射
            "credential_bound": False,  # 目标环境是否已绑定凭证
            "received_at": _now(),
            "activated_at": None,
            "re_uploaded": False,
            "integrity": {"passed": True, "checked_at": _now()},
            "_frozen_objects": frozen_objects,
        }
        store.rows(RECEIPT_MODULE).append(receipt)
        return receipt, ""

    def verify_integrity(self, file_payload: dict[str, Any]) -> tuple[bool, str]:
        """文件完整性验证：格式、身份、对象指纹与整体摘要逐项核对。"""
        if not isinstance(file_payload, dict):
            return False, "方案文件不是合法的 JSON 对象"
        if file_payload.get("format") != PACKAGE_FORMAT:
            return False, f"格式标识不匹配，期望 {PACKAGE_FORMAT}"
        if not file_payload.get("package_uid"):
            return False, "方案文件缺少独立身份 package_uid"
        objects = file_payload.get("objects")
        if not isinstance(objects, list) or not objects:
            return False, "方案文件不含任何冻结对象"
        # 逐对象核对指纹，防止文件被局部篡改
        for obj in objects:
            obj_type = str(obj.get("type") or "")
            if obj_type not in OBJECT_TYPES:
                return False, f"发现未知对象类型：{obj_type}"
            content = obj.get("content")
            if not isinstance(content, dict):
                return False, f"对象 {obj.get('name')} 缺少内容快照"
            actual_fp = fingerprint(obj_type, content)
            if actual_fp != obj.get("fingerprint"):
                return False, f"对象 {obj.get('name')} 指纹校验失败，文件可能已损坏或被篡改"
        # 整体摘要核对
        actual_digest = content_digest(file_payload)
        if actual_digest != file_payload.get("content_digest"):
            return False, (
                f"内容摘要不一致（期望 {file_payload.get('content_digest')[:12]} / "
                f"实际 {actual_digest[:12]}），完整性验证未通过"
            )
        if file_payload.get("secret_policy") != "credentials_excluded_bind_on_target":
            return False, "方案文件的秘密策略声明缺失或不被信任"
        return True, "完整性验证通过"

    def decide_node(self, receipt_id: int, fingerprint_value: str, decision: str) -> tuple[dict[str, Any] | None, str]:
        """对同名对象逐项决定复用还是另建；未决引用不清零则不能启用。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"接收记录 {receipt_id} 不存在"
        if receipt["status"] in (RECEIPT_STATUSES[2], RECEIPT_STATUSES[3]):
            return None, "接收记录已结束，不能再调整差异决策"
        if decision not in (NODE_REUSE, NODE_CREATE):
            return None, f"决策只支持「{NODE_REUSE}」或「{NODE_CREATE}」"
        node = next((n for n in receipt["diff_tree"] if n["fingerprint"] == fingerprint_value), None)
        if node is None:
            return None, f"差异树中找不到指纹 {fingerprint_value} 对应的对象"
        if not node["same_name_candidates"]:
            return None, f"对象 {node['name']} 在目标环境没有同名对象，只能另建，不能复用"
        node["decision"] = decision
        node["decided_at"] = _now()
        unresolved = sum(1 for n in receipt["diff_tree"] if n["decision"] == NODE_UNDECIDED)
        receipt["status"] = RECEIPT_STATUSES[0] if unresolved else RECEIPT_STATUSES[1]
        return receipt, ""

    def bind_credential(self, receipt_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """目标环境另行绑定凭证：认证秘密不随包转移，绑定后才具备执行资格。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"接收记录 {receipt_id} 不存在"
        credential_ref = str(values.get("credential_ref") or "").strip()
        if not credential_ref:
            return None, "请选择目标环境中的凭证（填写凭证编号）"
        auth_row = self._find_by_name("auth", credential_ref)
        if auth_row is None:
            return None, f"目标环境不存在凭证 {credential_ref}，无法绑定"
        binding = {
            "id": store.next_id(CREDENTIAL_MODULE),
            "receipt_id": receipt_id,
            "package_uid": receipt["package_uid"],
            "credential_ref": credential_ref,
            "auth_id": int(auth_row["id"]),
            "bound_at": _now(),
        }
        store.rows(CREDENTIAL_MODULE).append(binding)
        receipt["credential_bound"] = True
        receipt["credential_binding"] = binding
        return receipt, ""

    def activate(self, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        """启用方案：未决引用与凭证缺失都会阻止；导入只新建/复用，绝不替换现用配置。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"接收记录 {receipt_id} 不存在"
        if receipt["status"] == RECEIPT_STATUSES[2]:
            return receipt, "方案已启用，请勿重复操作"

        unresolved = [n["name"] for n in receipt["diff_tree"] if n["decision"] == NODE_UNDECIDED]
        if unresolved:
            return None, f"存在未决引用（{'、'.join(unresolved)}），阻止整包启用"
        if not receipt["credential_bound"]:
            return None, "目标环境尚未绑定凭证，方案不具备执行资格，阻止启用"

        # 幂等：已有映射说明对象已迁入，不重复复制
        if not receipt["object_mapping"]:
            mapping = self._materialize(receipt)
            receipt["object_mapping"] = mapping
        receipt["status"] = RECEIPT_STATUSES[2]
        receipt["activated_at"] = _now()
        return receipt, ""

    def reject(self, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"接收记录 {receipt_id} 不存在"
        if receipt["status"] == RECEIPT_STATUSES[2]:
            return None, "方案已启用，不能拒绝"
        receipt["status"] = RECEIPT_STATUSES[3]
        return receipt, ""

    def bind_job(self, job_id: int, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        """新采集作业绑定已启用方案；在途作业不被切换，保留原方案证据。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"接收记录 {receipt_id} 不存在"
        if receipt["status"] != RECEIPT_STATUSES[2]:
            return None, "方案尚未启用，不能绑定采集作业"
        job = store.find("spider", job_id)
        if job is None:
            return None, f"采集作业 {job_id} 不存在"
        if job.get("status") in ("运行中",):
            return None, "作业在途，不能切换方案；新方案只供之后建立的作业使用"
        # 若作业此前已绑定别的方案，先留存原方案证据
        if job.get("solution_uid") and job["solution_uid"] != receipt["package_uid"]:
            store.rows(EVIDENCE_MODULE).append({
                "id": store.next_id(EVIDENCE_MODULE),
                "job_id": job_id,
                "job_name": job.get("作业编号"),
                "previous_solution_uid": job["solution_uid"],
                "previous_digest": job.get("solution_digest"),
                "retained_at": _now(),
            })
        job["solution_uid"] = receipt["package_uid"]
        job["solution_digest"] = receipt["content_digest"]
        job["solution_receipt_id"] = receipt_id
        return job, ""

    def list_job_evidence(self) -> list[dict[str, Any]]:
        return store.rows(EVIDENCE_MODULE)

    def list_credentials(self) -> list[dict[str, Any]]:
        """目标环境可选凭证清单：供接收端绑定下拉，秘密本身不参与发行。"""
        return [
            {
                "auth_id": int(row["id"]),
                "credential_ref": row.get("凭证编号"),
                "target_site": row.get("目标站点"),
                "auth_type": row.get("认证方式"),
                "status": row.get("status"),
            }
            for row in store.rows("auth")
        ]

    # ------------------------------------------------------------------ 内部辅助

    def _digest_of(self, package: dict[str, Any]) -> str:
        body = {
            "format": PACKAGE_FORMAT,
            "package_id": package["package_uid"],
            "objects": package["frozen"],
            "references": package["references"],
            "secret_policy": "credentials_excluded_bind_on_target",
        }
        return content_digest(body)

    def _resolve_reference_name(self, parent_type: str, child_type: str, row: dict[str, Any]) -> str | None:
        """种子数据缺少真实外键，按业务含义用名称字段尽力解析被引用对象名。"""
        mapping = {
            ("template", "site"): "入口地址",
            ("template", "extractor"): "字段映射",
            ("schedule", "site"): "关联站点",
        }
        field = mapping.get((parent_type, child_type))
        if not field:
            return None
        value = str(row.get(field) or "").strip()
        # 种子值是“xx样例N”，与子对象编号对不上时返回 None，视为无硬引用
        if child_type == "site" and value.startswith("SITE-"):
            return value
        if child_type == "extractor" and value.startswith("EXTR-"):
            return value
        return None

    def _find_by_name(self, obj_type: str, name: str) -> dict[str, Any] | None:
        # auth 不进 OBJECT_TYPES（它不是可发行对象），但凭证绑定需要按编号反查
        extra_name_fields = {"auth": "凭证编号"}
        if obj_type in OBJECT_TYPES:
            module, name_field = OBJECT_TYPES[obj_type]
        elif obj_type in extra_name_fields:
            module, name_field = obj_type, extra_name_fields[obj_type]
        else:
            return None
        for row in store.rows(module):
            if str(row.get(name_field, "")) == name:
                return row
        return None

    def _find_receipt_by_uid(self, uid: str) -> dict[str, Any] | None:
        for receipt in store.rows(RECEIPT_MODULE):
            if receipt.get("package_uid") == uid:
                return receipt
        return None

    def preview_diff(self, file_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """只读预览引用差异树，供完整性验证接口调用，不产生任何落库副作用。"""
        return self._build_diff_tree(file_payload)

    def _build_diff_tree(self, file_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """展开引用差异树：按同名匹配列出候选，逐项标记复用/另建/未决。

        绝不在此阶段写入或替换任何现用配置，只做对比预览。
        """
        tree: list[dict[str, Any]] = []
        for obj in file_payload.get("objects", []):
            obj_type = str(obj["type"])
            name = str(obj.get("name") or "")
            module, name_field = OBJECT_TYPES[obj_type]
            candidates = [
                {
                    "target_id": int(row["id"]),
                    "name": row.get(name_field),
                    "version": str(row.get("模板版本", row.get("规则版本", "v1"))),
                    "fingerprint": fingerprint(obj_type, row),
                    "content_equal": fingerprint(obj_type, row) == obj.get("fingerprint"),
                }
                for row in store.rows(module)
                if str(row.get(name_field, "")) == name
            ]
            exact = next((c for c in candidates if c["content_equal"]), None)
            if exact is not None:
                # 目标环境存在内容完全一致的同名对象：默认复用，无需人工决策
                decision = NODE_REUSE
            elif candidates:
                # 同名但版本/内容不同：必须人工逐项裁决
                decision = NODE_UNDECIDED
            else:
                # 目标环境无同名对象：只能另建
                decision = NODE_CREATE
            tree.append({
                "type": obj_type,
                "name": name,
                "source_version": obj.get("version"),
                "fingerprint": obj.get("fingerprint"),
                "same_name_candidates": candidates,
                "decision": decision,
                "target_id": exact["target_id"] if exact else None,
                "decided_at": None,
            })
        return tree

    def _materialize(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        """按差异树决策落地对象：复用只记录绑定，另建才复制新对象；不触碰现用配置。"""
        # 用上传文件中的冻结内容按指纹建索引
        frozen_index = {node["fingerprint"]: node for node in self._frozen_nodes(receipt)}
        mapping: list[dict[str, Any]] = []
        for node in receipt["diff_tree"]:
            obj_type = node["type"]
            module, name_field = OBJECT_TYPES[obj_type]
            frozen = frozen_index.get(node["fingerprint"])
            if node["decision"] == NODE_REUSE:
                target_id = node.get("target_id")
                if target_id is None and node["same_name_candidates"]:
                    target_id = int(node["same_name_candidates"][0]["target_id"])
                node["decision"] = NODE_REUSED
            else:
                # 另建：深拷贝冻结内容，分配新主键与新编号，避免覆盖同名现用对象
                base = copy.deepcopy(frozen["content"]) if frozen else {}
                new_id = store.next_id(module)
                base["id"] = new_id
                base[name_field] = f"{node['name']}-COPY{new_id}"
                base["status"] = "草稿"
                base["pending"] = True
                base["abnormal"] = False
                store.rows(module).append(base)
                target_id = new_id
                node["decision"] = NODE_CREATED
            node["target_id"] = target_id
            mapping.append({
                "type": obj_type,
                "source_fingerprint": node["fingerprint"],
                "source_name": node["name"],
                "target_id": target_id,
                "target_name": store.find(module, int(target_id)).get(name_field) if target_id else None,
                "resolution": node["decision"],
            })
        return mapping

    def _frozen_nodes(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        """从接收记录反查冻结对象：接收时已把文件对象存进 receipt 内部上下文。"""
        return receipt.get("_frozen_objects", [])
