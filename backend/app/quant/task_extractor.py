"""定时任务报告 → 协同任务提取器

从定时任务 Agent 的报告中提取需要其他数字人协同完成的任务。
支持两种输入：
1. structured_tool 模式：Agent 直接调用 create_agent_task_batch 工具（无需提取器）
2. legacy_report 模式：从 Markdown 报告中用 LLM 提取候选任务

提取结果必须符合严格 JSON Schema，经过服务端校验后才创建任务。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config
from backend.app.core.models import DigitalHumanProfile, JobRun
from backend.app.quant.agent_board_service import TaskBatchSpec, TaskSpec

logger = logging.getLogger(__name__)

# 提取结果的 JSON Schema
EXTRACTION_PROMPT = """你是一个任务提取器。从以下定时任务报告中，提取需要其他数字人继续完成的动作。

## 输出格式

严格输出 JSON，不要包含任何其他文字：

{{
  "has_tasks": true,
  "confidence": 0.95,
  "tasks": [
    {{
      "action_key": "动作-标的-日期",
      "title": "看板卡片标题",
      "agent_code": "数字人代码",
      "instruction": "可独立完成的子任务指令",
      "expected_output": "期望返回的结构",
      "required": true,
      "priority": 100
    }}
  ],
  "warnings": []
}}

## 提取规则

1. 只提取明确要求"Agent 自动执行"的动作，不要提取"建议用户做"的内容
2. agent_code 必须是已知的数字人代码（见下方列表）
3. action_key 格式：动作-标的-日期，如 verify-600984-20260801
4. confidence 表示提取结果的置信度（0-1），低于 0.8 的结果不会自动执行
5. 如果报告中没有需要 Agent 执行的动作，返回 {{"has_tasks": false, "confidence": 1.0, "tasks": [], "warnings": []}}

## 可用的数字人代码

{agent_codes}

## 报告内容

{report_content}
"""


class ScheduledTaskExtractor:
    """从定时任务报告中提取协同任务"""

    def extract(
        self,
        output_content: str,
        *,
        job_run_id: str,
        max_tasks: int = 20,
    ) -> dict[str, Any]:
        """
        从报告输出中提取候选任务。

        Returns:
            {
                "has_tasks": bool,
                "confidence": float,
                "tasks": [...],
                "warnings": [...],
            }
        """
        if not output_content or not output_content.strip():
            return {"has_tasks": False, "confidence": 1.0, "tasks": [], "warnings": ["报告内容为空"]}

        # 获取可用数字人列表
        agent_codes = self._get_agent_codes()
        if not agent_codes:
            return {"has_tasks": False, "confidence": 1.0, "tasks": [], "warnings": ["没有可用的数字人"]}

        # 构建提取 prompt
        prompt = EXTRACTION_PROMPT.format(
            agent_codes="\n".join(f"- {code}: {name}" for code, name in agent_codes.items()),
            report_content=output_content[:8000],  # 截断避免超长
        )

        # 调用 LLM 提取
        try:
            result = self._call_llm(prompt)
        except Exception as exc:
            logger.warning("任务提取 LLM 调用失败: %s", exc)
            return {"has_tasks": False, "confidence": 0.0, "tasks": [], "warnings": [f"LLM 调用失败: {exc}"]}

        # 校验结果
        validated = self._validate(result, agent_codes, max_tasks)

        # 保存到 JobRun.handoff_plan_json
        self._save_extraction_plan(job_run_id, validated)

        return validated

    def to_task_batch(
        self,
        plan: dict[str, Any],
        *,
        objective: str = "定时任务协同任务",
        completion_policy: str = "all_required",
        child_timeout_seconds: int = 600,
    ) -> TaskBatchSpec:
        """将提取结果转为 TaskBatchSpec"""
        tasks = [
            TaskSpec(
                action_key=str(t["action_key"]),
                title=str(t["title"]),
                agent_code=str(t["agent_code"]),
                instruction=str(t["instruction"]),
                expected_output=str(t.get("expected_output") or ""),
                required=bool(t.get("required", True)),
                priority=int(t.get("priority", 100)),
            )
            for t in plan.get("tasks", [])
        ]
        return TaskBatchSpec(
            objective=objective,
            execution_mode="serial_items",
            completion_policy=completion_policy,
            child_timeout_seconds=child_timeout_seconds,
            tasks=tasks,
        )

    def _get_agent_codes(self) -> dict[str, str]:
        """获取可用的数字人代码列表"""
        with SessionLocal() as db:
            profiles = db.scalars(
                select(DigitalHumanProfile).where(
                    DigitalHumanProfile.status == "enabled",
                    DigitalHumanProfile.delegation_enabled.is_(True),
                )
            ).all()
            return {p.code: p.name for p in profiles}

    def _call_llm(self, prompt: str) -> dict[str, Any]:
        """调用 LLM 提取任务"""
        import httpx

        config = active_llm_config()
        if config is None:
            raise RuntimeError("没有可用的 LLM 配置")

        url = f"{config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": "你是任务提取器，严格输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"].strip()
        # 提取 JSON（可能被 ```json ``` 包裹）
        json_match = re.search(r"\{[\s\S]*\}", content)
        if not json_match:
            raise ValueError(f"LLM 返回内容不是有效 JSON: {content[:200]}")

        return json.loads(json_match.group())

    def _validate(
        self,
        result: dict[str, Any],
        agent_codes: dict[str, str],
        max_tasks: int,
    ) -> dict[str, Any]:
        """校验提取结果"""
        warnings = list(result.get("warnings") or [])

        if not isinstance(result.get("has_tasks"), bool):
            warnings.append("has_tasks 不是布尔值，默认 false")
            return {"has_tasks": False, "confidence": 0.0, "tasks": [], "warnings": warnings}

        if not result["has_tasks"]:
            return {"has_tasks": False, "confidence": float(result.get("confidence", 1.0)), "tasks": [], "warnings": warnings}

        confidence = float(result.get("confidence", 0.0))
        raw_tasks = result.get("tasks") or []
        if not isinstance(raw_tasks, list):
            warnings.append("tasks 不是数组")
            return {"has_tasks": False, "confidence": confidence, "tasks": [], "warnings": warnings}

        # 校验每个任务
        validated_tasks = []
        seen_keys = set()
        for i, task in enumerate(raw_tasks):
            if not isinstance(task, dict):
                warnings.append(f"任务 {i} 不是对象，跳过")
                continue

            # 必填字段
            action_key = str(task.get("action_key") or "").strip()
            title = str(task.get("title") or "").strip()
            agent_code = str(task.get("agent_code") or "").strip()
            instruction = str(task.get("instruction") or "").strip()

            if not action_key or not title or not agent_code or not instruction:
                warnings.append(f"任务 {i} 缺少必填字段，跳过")
                continue

            # action_key 唯一性
            if action_key in seen_keys:
                warnings.append(f"action_key 重复: {action_key}，跳过")
                continue
            seen_keys.add(action_key)

            # agent_code 存在性
            if agent_code not in agent_codes:
                warnings.append(f"agent_code 不存在: {agent_code}，跳过")
                continue

            validated_tasks.append({
                "action_key": action_key,
                "title": title[:240],
                "agent_code": agent_code,
                "instruction": instruction,
                "expected_output": str(task.get("expected_output") or ""),
                "required": bool(task.get("required", True)),
                "priority": int(task.get("priority", 100)),
            })

            if len(validated_tasks) >= max_tasks:
                warnings.append(f"任务数量超过上限 {max_tasks}，已截断")
                break

        return {
            "has_tasks": len(validated_tasks) > 0,
            "confidence": confidence,
            "tasks": validated_tasks,
            "warnings": warnings,
        }

    def _save_extraction_plan(self, job_run_id: str, plan: dict[str, Any]) -> None:
        """保存提取结果到 JobRun.handoff_plan_json"""
        with SessionLocal() as db:
            job_run = db.get(JobRun, job_run_id)
            if job_run is not None:
                job_run.handoff_plan_json = json.dumps(plan, ensure_ascii=False, default=str)
                db.commit()
