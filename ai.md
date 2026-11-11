# AI Usage Report

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
checked the result against the plan before the next step.

`PLAN.md` holds the approved goals and the safety gates. `agent-plan.md` is the
coordinator brief that Terra wrote for the sub-agents. Together they are the
interface between the two models.

## What AI produced

- Everything! After all, if you want to develop a working prototype in such a short amount of time, you don't have the time to learn Lua or how to load CPython onto a Kindle.

## Usage Report
**Terra** - 72.9M tokens with total cost of $9.66

**Deepseek V4.1** -  459.4M with a total cost of $21.38


