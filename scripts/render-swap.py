"""Render one reviewer definition with the selected resource profile."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import ROOT, load_config

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    args = parser.parse_args()
    config = load_config(ROOT / "config/harness.json")
    profile = args.profile or config["review_profile"]
    selected = config["review_profiles"][profile]
    path = Path(selected["hot_experts"])
    if not path.is_file():
        raise SystemExit(f"Missing expert file: {path}")
    template = (ROOT / "config/llama-swap.yaml").read_text(encoding="utf-8")
    if template.count("@REVIEW_HOT_EXPERTS@") != 1:
        raise SystemExit("Expected exactly one expert-file placeholder")
    escaped = json.dumps(path.as_posix())[1:-1]
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs/swap-profile.yaml").write_text(template.replace("@REVIEW_HOT_EXPERTS@", escaped), encoding="utf-8")
    (ROOT / "runs/swap-profile.json").write_text(json.dumps({"profile": profile, **selected}), encoding="utf-8")
    print(f"Rendered {profile}: {path}")
