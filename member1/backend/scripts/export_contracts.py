"""Regenerate portable schemas and a clearly labelled synthetic teammate fixture."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.modules.intelligence.contracts import (AnalysisRequest, AnalysisResult, ContextDraft, DebateRequest,
    DebateSession, IdeaSpec, Job, JobReceipt, ProjectContext, ResearchOptions, utc_now)
from app.modules.intelligence.figures import extract_figures
from app.modules.intelligence.pipeline import build_service
from app.modules.intelligence.settings import Settings


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def main():
    output = BACKEND.parent.parent / "integration"
    for contract in (IdeaSpec, ContextDraft, ProjectContext, AnalysisRequest, ResearchOptions, AnalysisResult, DebateRequest, DebateSession, Job, JobReceipt):
        write(output / "schemas" / f"{contract.__name__}.schema.json", contract.model_json_schema())
    service = build_service(Settings(demo_delay=0))
    idea = await service.extract("Campus Meals helps college students find affordable nutritious meals through local kitchens and scheduled campus pickup.")
    idea.project_name = "Campus Meals — synthetic integration fixture"
    idea.problem = "College students need a convenient way to find affordable, nutritious meals near campus."
    idea.domain = "Food services"
    result = await service.analyze("p_integration_demo", idea, ResearchOptions())
    figures = extract_figures(result.context_draft.evidence, result.context_draft.sources)
    result.figures = figures
    result.context_draft.figures = figures
    result = AnalysisResult.model_validate(result.model_dump())
    payload = result.context_draft.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    context = ProjectContext(**payload, project_id="p_integration_demo", context_version=1, created_at=utc_now(), content_hash=digest)
    write(output / "fixtures" / "project_context.demo.json", context.model_dump(mode="json"))
    write(output / "fixtures" / "analysis_result.demo.json", result.model_dump(mode="json"))
    print(f"Exported ten schemas and two synthetic fixtures to {output}")


if __name__ == "__main__":
    asyncio.run(main())
