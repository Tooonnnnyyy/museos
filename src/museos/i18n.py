"""Languages for model output and for the few notes the system writes itself.

Three languages are supported: English (default), French, Simplified Chinese.
JSON keys, task ids, role ids, status values and technical error strings stay in English.
"""

from __future__ import annotations

from typing import Literal

Language = Literal["en", "fr", "zh"]
LANGUAGE_NAMES = {"en": "English", "fr": "French", "zh": "Simplified Chinese"}


def language_rule(language: str | None) -> str:
    """Instruction appended to every prompt. None = follow the language of the goal."""
    keep = "Keep JSON keys and the values of task_id, role, verdict and confidence exactly as specified."
    if language in LANGUAGE_NAMES:
        return (f"Write every other text value in {LANGUAGE_NAMES[language]}, whatever language the goal or the "
                f"other inputs are in. {keep}")
    return f"Write every other text value in the same language as the project goal. {keep}"


_NOTES: dict[str, dict[str, str]] = {
    "partial": {
        "en": "Partial result: {n} of {total} tasks did not complete ({detail}).",
        "fr": "Résultat partiel : {n} tâche(s) sur {total} n'ont pas abouti ({detail}).",
        "zh": "部分结果：{total} 个任务中有 {n} 个没有完成（{detail}）。",
    },
    "template_plan": {
        "en": "The task plan came from a fixed template, not the Chief model.",
        "fr": "Le plan de tâches vient d'un modèle fixe, pas du modèle Chief.",
        "zh": "任务计划来自固定模板，不是 Chief 模型生成的。",
    },
    "unknown_cost": {
        "en": "{n} call(s) had unknown cost; the budget booked the reserved ceiling for them.",
        "fr": "{n} appel(s) au coût inconnu ; le budget a compté le plafond réservé.",
        "zh": "有 {n} 次调用的费用未知，预算按预留上限计入。",
    },
    "mock_used": {
        "en": "Mock providers were used: section content is placeholder text, not real analysis.",
        "fr": "Des fournisseurs simulés ont été utilisés : le contenu est du texte factice, pas une vraie analyse.",
        "zh": "使用了模拟模型：各部分内容是占位文字，不是真实分析。",
    },
    "same_reviewer": {
        "en": "Review of {task} used the same provider as the author (no independent reviewer available).",
        "fr": "La relecture de {task} a utilisé le même fournisseur que l'auteur (aucun relecteur indépendant disponible).",
        "zh": "{task} 的审查和作者用的是同一个模型（没有可用的独立审查者）。",
    },
    "synthesis_failed": {
        "en": "Synthesis model call failed ({error}); summary is deterministic.",
        "fr": "L'appel du modèle de synthèse a échoué ({error}) ; le résumé est généré sans modèle.",
        "zh": "合成模型调用失败（{error}），摘要由程序直接拼接。",
    },
    "fallback_summary": {
        "en": "{done} of {total} specialist tasks completed for: {goal}.",
        "fr": "{done} tâche(s) de spécialiste sur {total} terminée(s) pour : {goal}.",
        "zh": "{total} 个专家任务完成了 {done} 个，目标：{goal}。",
    },
    "plan_call_failed": {
        "en": "Chief planning call failed ({error}); a fixed template plan was used.",
        "fr": "L'appel de planification du Chief a échoué ({error}) ; un plan fixe a été utilisé.",
        "zh": "Chief 规划调用失败（{error}），改用固定模板计划。",
    },
    "no_planner": {
        "en": "No healthy provider for planning; a fixed template plan was used.",
        "fr": "Aucun fournisseur disponible pour planifier ; un plan fixe a été utilisé.",
        "zh": "没有可用的模型做规划，改用固定模板计划。",
    },
    "fallback_provider": {
        "en": "Primary provider {primary} failed; result came from fallback {fallback}.",
        "fr": "Le fournisseur principal {primary} a échoué ; le résultat vient du fournisseur de secours {fallback}.",
        "zh": "首选模型 {primary} 失败，结果来自备用模型 {fallback}。",
    },
    "missing_capabilities": {
        "en": "Selected provider lacks requested capabilities: {caps}",
        "fr": "Le fournisseur choisi n'a pas les capacités demandées : {caps}",
        "zh": "所选模型缺少任务要求的能力：{caps}",
    },
    "no_evidence": {
        "en": "No external evidence was retrieved in this run.",
        "fr": "Aucune preuve externe n'a été récupérée pendant cette exécution.",
        "zh": "这次运行没有检索任何外部证据。",
    },
}

_HEADINGS: dict[str, dict[str, str]] = {
    "title": {"en": "MuseOS deliverable — run", "fr": "Livrable MuseOS — exécution", "zh": "MuseOS 交付物 — 运行"},
    "goal": {"en": "Goal", "fr": "Objectif", "zh": "目标"},
    "status": {"en": "Status", "fr": "Statut", "zh": "状态"},
    "providers": {"en": "Providers", "fr": "Fournisseurs", "zh": "模型"},
    "cost": {"en": "Cost (est.)", "fr": "Coût (est.)", "zh": "费用（估算）"},
    "tokens": {"en": "Tokens", "fr": "Tokens", "zh": "Tokens"},
    "summary": {"en": "Executive summary", "fr": "Résumé", "zh": "摘要"},
    "agreements": {"en": "Agreements", "fr": "Points d'accord", "zh": "共识"},
    "disagreements": {"en": "Disagreements", "fr": "Points de désaccord", "zh": "分歧"},
    "next_steps": {"en": "Next steps", "fr": "Prochaines étapes", "zh": "下一步"},
    "recommendations": {"en": "Recommendations", "fr": "Recommandations", "zh": "建议"},
    "assumptions": {"en": "Assumptions", "fr": "Hypothèses", "zh": "假设"},
    "risks": {"en": "Risks", "fr": "Risques", "zh": "风险"},
    "evidence": {"en": "Evidence", "fr": "Preuves", "zh": "证据"},
    "failed": {"en": "Failed / skipped tasks", "fr": "Tâches échouées ou ignorées", "zh": "失败或跳过的任务"},
    "limitations": {"en": "Limitations", "fr": "Limites", "zh": "局限"},
}


def _pick(table: dict[str, dict[str, str]], language: str | None, key: str) -> str:
    return table[key].get(language or "en", table[key]["en"])


def note(language: str | None, key: str, **values: object) -> str:
    return _pick(_NOTES, language, key).format(**values)


def heading(language: str | None, key: str) -> str:
    return _pick(_HEADINGS, language, key)
