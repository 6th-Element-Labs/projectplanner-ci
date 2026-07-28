from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / "fixtures" / "mission_bot_canaries" / "COORD-101.txt"

assert fixture.read_text(encoding="utf-8") == "hello world from COORD-101\n"

print("CANARY_EXPECTED_RED: COORD-101 first implementation generation")
raise SystemExit(1)
