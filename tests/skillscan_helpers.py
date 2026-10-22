"""Fake SkillSpector verdicts and hub builders shared by test_skillscan.py and test_quarantine.py."""

REPORTS = {"evil": {"recommendation": "DO_NOT_INSTALL", "score": 100, "max_severity": "CRITICAL"},
           "risky": {"recommendation": "CAUTION", "score": 40, "max_severity": "HIGH"},
           "fine": {"recommendation": "SAFE", "score": 0, "max_severity": None},
           "broken": {"error": "exit 2: unreadable"}}


def make_skills(root, *names):
    out = []
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: test\n---\n", encoding="utf-8")
        out.append(root / n)
    return out


def apply_act(msg, fn=None):
    if fn:
        fn()


def all_of(items):
    return {i["name"] for i in items}
