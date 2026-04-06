
## Health Stack

- lint: ruff check .
- test: python3 -m pytest tests/
- shell: shellcheck autostart.sh
- ui: ./scripts/deep_qa_test.sh

## UI Validation Mandate

- NEVER settle for "page loaded" status.
- ALWAYS simulate user interaction (click "Run Backtest", "Run Comparison").
- ALWAYS check for runtime errors by scanning page text for "Traceback" or "KeyError".
- Use `gstack-browse` to capture screenshots AFTER execution completes.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
