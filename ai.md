# AI usage report

AI wrote most of this project with me: the research, the prototypes and most of
the iteration. I ran every hardware test, made the decisions that carried risk,
and reviewed each change. We optimised for a working prototype, so the receiver
and the plugin state machine carry a 315-test suite while the hardware paths are
verified by hand, and the security model is deliberately limited to a trusted
LAN.

## Setup

The harness was the Pi CLI with two models:

- **GPT 6 Terra** did the planning and the orchestration.
- **DeepSeek V4.1 1017** did the implementation and the targeted research.

Terra is the stronger planner and has a good cost per task. DeepSeek is cheaper
and less capable, but strong at code. Terra read the goal, wrote the plan and
the safety boundaries, and split the work into briefs. It then started DeepSeek
sub-agents, one brief each, and checked each result against the plan before the
next step.

`PLAN.md` holds the approved goals and the safety gates. `agent-plan.md` is the
coordinator brief that Terra wrote for the sub-agents. Together they are the
interface between the two models.

## What AI produced

- The receiver, the Kindle capture daemon, the KOReader plugin, the test suite,
  and the first drafts of these docs.

I ran the hardware, chose the architecture and the safety gates, and corrected
the docs against the code and the hardware results. Building a working prototype
in the time available meant not learning Lua or the Kindle CPython bootstrap
from scratch.

## How the output was checked

I reviewed every diff, ran the suite after each change, and rejected output that
did not match the hardware results. The models could not test hardware, so every
hardware claim in these docs comes from my own runs. The AI drafted the
documentation; I checked each number and command against the code and the logs.

## Usage report

| Model | Role | Tokens | Cost |
| --- | --- | ---: | ---: |
| GPT 6 Terra | planning, orchestration | 72.9M | $9.66 |
| DeepSeek V4.1 1017 | implementation, research | 459.4M | $21.38 |
| **Total** | | **532.3M** | **$31.04** |
