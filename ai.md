# How I used AI

AI wrote this project with me. It did the research, the prototypes and most of
the iteration. I ran the hardware and made the decisions that carried risk. As this project
was more about getting an MVP up and running, there was definitely a focus on iterating
as quickly as possible, so we kept testing and security restrictions minimal.

## Setup

The harness used was the Pi CLI with two models:

- **GPT 6 Terra** did the planning and the orchestration.
- **DeepSeek V4.1 1017** did the implementation and the targeted research.

GPT 6 Terra is the smarter model that has a good cost-per-task, while DeepSeek V4.1 is less intelligent but still shows strong coding ability with an exceptionally low cost-per-task.

The idea was to let the stronger model control the cheaper, general-purpose
models. Terra read the goal, wrote the plan and the safety boundaries, and split
the work into briefs. It then started DeepSeek sub-agents, one brief each, and
checked the result against the plan before the next step. A sub-agent got a
narrow job: one file, one test, or one question with a defined answer.

`PLAN.md` holds the approved goals and the safety gates. `agent-plan.md` is the
coordinator brief that Terra wrote for the sub-agents. Together they are the
interface between the two models.

## What AI produced

- **Research.** Bluetooth HFP, Bumble, controller ownership on the Kindle, and
  the OpenRouter audio models. The model comparison and the measured cost per
  clip in [docs/transcription.md](docs/transcription.md) come from this work.
- **Prototypes.** The receiver, the Kindle capture supervisor, the KOReader
  plugin, the audio tools, and the tests. About 14,000 lines of Python and Lua,
  and 2,400 lines of Markdown.
- **Iteration.** Each result from the device changed the plan. A failed test
  became a new constraint in `PLAN.md` and a new brief for a sub-agent.

## What AI did not do

AI never touched the Kindle. I ran every device command and returned the output
as text. The results in [docs/hardware-tests.md](docs/hardware-tests.md) are
observations from the device, not model output. I decided what to test and
accepted the risk of each Bluetooth handoff.
