"""Export additive integration schemas without replacing the existing fixture."""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.integration.contracts import (ContextHandoff, ContextReference, GenerationResult, ModuleStatus,
                                      PitchDeckRequest, PosterRequest, ReportGenerationRequest, TemplateDescriptor)


def main():
    output = BACKEND.parents[1] / "integration" / "schemas"
    output.mkdir(parents=True, exist_ok=True)
    for contract in (ContextHandoff, ContextReference, GenerationResult, ModuleStatus, PitchDeckRequest,
                     PosterRequest, ReportGenerationRequest, TemplateDescriptor):
        target = output / f"{contract.__name__}.schema.json"
        target.write_text(json.dumps(contract.model_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Exported eight integration schemas to {output}")


if __name__ == "__main__":
    main()
