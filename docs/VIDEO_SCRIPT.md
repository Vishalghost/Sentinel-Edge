# Demo video script (target 2:45)

Record at 1080p. Screens: the dashboard (browser), the WSL terminal running the edge agent, and the architecture image. Start the Decider server and the edge agent before recording.

| time | on screen | voice-over |
|---|---|---|
| 0:00-0:15 | Dashboard, empty stream; pan over the KPI row | "When one pump trips in a chemical plant, operators get forty alarms in a minute, and the one that matters can scroll right past. This agent decides which alarms a person actually needs to see." |
| 0:15-0:35 | Architecture image; point to the laptop, then AWS | "The decision runs on the edge: Strands Decider 2B on this laptop's GPU, standing in for a factory edge box. It connects outbound only to AWS IoT Core. The cloud stores every decision and calls Amazon Bedrock only for real incidents." |
| 0:35-1:15 | Click **Start alarm flood** (Solvent Recovery Unit, cooling failure). Split screen: dashboard plus edge terminal logs | "Every alarm arrives over MQTT. Decider answers four yes-or-no questions about it: is it dangerous, a consequence of something active, chattering, under maintenance? You can see the four bars per alarm. Python turns those into a route. The chattering level switch: suppressed. The condenser temperature: recognised as a consequence of the cooling pump. The relief valve lifting: danger 0.76, escalated." |
| 1:15-1:40 | The incident card appears; read the root cause | "Only escalated alarms reach the LLM. A Strands agent on Bedrock reads the alarm history and the operator log through tools, and writes the root cause: cooling water loss, condenser, column pressure, relief valve, with next steps." |
| 1:40-1:55 | Chat: type "Why was LSH-305 suppressed?" | "Operators can ask why, and the answer comes from the edge's actual decision." |
| 1:55-2:15 | Switch plant to the bottling line, start a flood | "Industries are YAML files. Same engine, a juice bottling line: the pasteuriser under temperature is a food-safety alarm, escalated immediately; the line starving behind it is not." |
| 2:15-2:40 | `bench/results/RESULTS.md` table | "Against an LLM classifying every alarm: same accuracy on 49 labelled alarms, nothing real hidden in either, but 78% fewer LLM calls, 92% fewer tokens, and faster decisions. The biggest lesson: give a small model only the context each question needs. That took held-out accuracy from 57 to 80%." |
| 2:40-2:50 | Repo page, `decider_policy/README.md` | "The PolicyGate pattern and its Strands intervention are in the repo, ready to drop into your own agent." |

Backup if AWS is down while recording: `python -m edge.local_run process_plant cooling_failure_cascade` shows the same decisions in the terminal.
