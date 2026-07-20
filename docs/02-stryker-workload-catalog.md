# 02 — Stryker Smart Care workload catalog

**The answer to "what else can we add?"** The "smart badge" is the **Vocera Smartbadge** —
Stryker acquired Vocera in 2022. That matters enormously, because Stryker already ships the
*entire* stack this demo imagines: a voice-first wearable, an FDA-cleared workflow/alarm
**middleware (Vocera Engage / EMDAN)** with **150+ documented integrations**, connected data
sources (ProCuity beds, LIFENET/LIFEPAK, Triton, physiologic monitors), and a **public adapter
catalog that reads like a menu of agent tools**. Nightingale sits *on top of* Engage as an
LLM orchestration layer.

> **Sources** are Stryker/Vocera primary product & technical docs plus one peer-reviewed
> nursing article for sepsis. Vendor figures ("150+", "99.99%") are marketing claims — label
> them as such to Stryker. Field-level device schemas, a Stryker-native RTLS, and an open
> third-party FHIR API are **not publicly documented** — mark them "to confirm with Stryker."

## Part A — The Stryker/Vocera platform (what emits data / exposes actions)

| Product | Category | Real-time data it emits / exposes | Actions an agent could invoke | Confidence |
|---|---|---|---|---|
| **Vocera Smartbadge** | Comms device / front door | Presence, do-not-disturb state, panic-button press, voice input, location (via RTLS) | Call/page by name·role·group, broadcast, escalate, set DND, push notification | HIGH |
| **Vocera Engage** | Workflow/alarm middleware | Routed & prioritized alarms + context (patient / event / care team); **Dynamic Master Directory** | Filter/route/escalate, query directory, trigger workflow rules | HIGH |
| **ProCuity bed** | Smart bed | Bed-exit alarm, weight/position-adaptive alarm, siderail/height compliance, nurse-call request | Read bed status; receive exit/compliance alerts (via **iBed adapter**) | HIGH (categories); wire schema not public |
| **LIFENET / LIFEPAK** | Emergency cloud | Incoming-patient pre-alert, 12-lead ECG, STEMI/stroke flags, device-readiness alerts | Notify/activate care team, retrieve ECG/case data, check device readiness | HIGH |
| **SurgiCount+ / Triton** | OR safety / hemorrhage | Sponge-count status, real-time **quantified blood loss**, hemorrhage threshold | Hemorrhage text-alert, coordinate transfusion response, EMR write | HIGH |
| **iSuite** | Connected OR | Device/video state (control fabric, not sensor telemetry) | Route video, control OR lights/devices | MODERATE |
| **Smart Equipment Mgmt / ProCare** | Asset / service | Device "last seen" location, health/wear trend, service status | Locate device, submit maintenance request, check readiness | HIGH |

**Design notes**
- **Engage is the brain.** It already does *filtering, routing, escalation and prioritization*
  and includes **EMDAN**, FDA **510(k)-cleared** secondary-alarm middleware. Your agent
  augments it (adds reasoning, multi-step workflows, conversation) — it does **not** suppress
  or reprioritize cleared alarms. Human-in-the-loop for anything clinical.
- **Bed → badge path is real.** The **iBed Adapter** carries ProCuity events into Engage and
  onto the Smartbadge — this *is* your "connect to a smart bed for real-time data" use case.
- **The adapter catalog is your tool menu** (see Part C). Model each agent tool as a thin
  wrapper over one adapter.

Sources: [Vocera Smartbadge](https://www.stryker.com/us/en/smart-care/products/vocera-smartbadge.html),
[Vocera Engage](https://www.stryker.com/us/en/smart-care/products/vocera-engage.html),
[ProCuity](https://www.stryker.com/us/en/acute-care/products/procuity.html),
[LIFENET](https://www.stryker.com/us/en/emergency-care/products/lifenet-system.html),
[SurgiCount+/Triton](https://www.stryker.com/us/en/surgical-technologies/products/surgicount-safety-sponge-system.html),
[iSuite](https://www.stryker.com/us/en/portfolios/medical-surgical-equipment/advanced-digital-healthcare/isuite.html),
[Smart Equipment Management](https://www.stryker.com/us/en/surgical/services/smart-equipment-management.html),
[Stryker–Vocera acquisition](https://www.stryker.com/us/en/about/news/2022/stryker-announces-definitive-agreement-to-acquire-vocera-communi.html).

## Part B — Workflow catalog (agent candidates)

Tagged 🔴 **Emergency / low-latency critical** or 🟢 **Routine**. The five you already named
are 1–5; 6–16 are the additions.

| # | Workflow | Class | Grounded integration surface |
|---|---|---|---|
| 1 | **Call a doctor / care team** | 🟢 (🔴 for codes) | Engage resolves *role → current person* via on-call adapters (AMiON/QGenda/Spok/Lightning Bolt/Shift Admin) + presence-aware escalation |
| 2 | **Prepare a hospital room / bed** | 🟢 | Engage bed-management / patient-flow / EVS integrations; ProCuity bed state (occupied / re-zeroed) |
| 3 | **Check supplies / blood products** | 🟢→🔴 | *Not a Stryker product* — Blood Bank/LIS via **HL7 Adapter** or **Scripted Adapter**; Triton QBL for hemorrhage trigger |
| 4 | **Retrieve clinical protocol** | 🟢→🔴 | Agent RAG (**Foundry IQ**) over the hospital protocol library + lab/vitals via HL7 / Patient Context |
| 5 | **Connect to smart stretcher/bed** | 🔴→🟢 | ProCuity bed-exit/position/weight/siderail via **iBed Adapter → Engage → badge** |
| 6 | **Code Blue / cardiac-arrest activation** | 🔴 | Smartbadge **panic button** → Engage escalation group + broadcast to code team; time-based escalation |
| 7 | **Rapid Response Team (RRT) activation** | 🔴 | Same escalation engine; agent gathers vitals context first, then pages RRT by role |
| 8 | **Sepsis alert + hour-1 bundle walkthrough** | 🔴 | HL7 (lactate/culture/labs) + Patient Context REST + voice guidance (see Part D) |
| 9 | **Fall / bed-exit response** | 🔴 (safety) | ProCuity **Adaptive Bed Alarm** → iBed adapter → nearest-nurse routing; agent reads weight/position |
| 10 | **Physiologic-monitor alarm triage** | 🔴 | Engage **EMDAN** + monitor adapters (GE Carescape, Nihon Kohden, Spacelabs, Sotera) deliver *secondary* alarm + context (augment only) |
| 11 | **Postpartum-hemorrhage / massive-transfusion coordination** | 🔴 | **Triton QBL** threshold → hemorrhage text-alert → mobilize blood + OB team |
| 12 | **STEMI / stroke incoming-patient pre-alert (ED)** | 🔴 | **LIFENET** pre-arrival notice + 12-lead → activate cath-lab / stroke team |
| 13 | **Locate / request equipment** | 🟢 | **SEM** "last seen location" + **ProCare** service request |
| 14 | **On-call / consult resolution** | 🟢 | On-call scheduling adapters resolve role→person; voice call/page |
| 15 | **Lab-result notification & acknowledgment** | 🟢 (🔴 critical values) | **HL7 Adapter** brings results; agent notifies assigned nurse w/ context; closed-loop callback |
| 16 | **Shift handoff / care-team lookup** | 🟢 | **Dynamic Master Directory** + assignment adapters (Rauland ResponderSync, Hillrom Clinical Staff API) — "who's covering bed 12?" |

**Recommended demo scope:** don't build all 16. Build the **shared spine** (gateway →
orchestrator → tools → hosted agents → control plane) plus **two flows**: one 🟢 routine
(#14 "call the on-call hospitalist" or #13 "find a working pump") to prove the loop, and one
🔴 emergency (**#8 sepsis** — best narrative — or #6 code blue / #9 fall) to show multi-agent
orchestration, parallelism, escalation, and low-latency routing.

## Part C — Integration surfaces (how agent tools connect)

The most useful artifact is **Vocera's public adapter catalog** — model each agent tool as a
wrapper over one of these.

| Interface | What it does | Agent-tool use |
|---|---|---|
| **HL7 Adapter** | Talks to any HL7-capable system; brings lab results, ADT, radiology | Read labs (lactate, cultures), results — backbone for EHR/LIS data |
| **Patient Context Adapter (REST)** | Find patients, find their care team, retrieve patient details | Cleanest REST tool: "who is this patient / who's their care team?" |
| **Clinical API Adapter** | Publish staff & device assignments per Clinical API spec | Sync care-team assignments in/out |
| **Scripted Adapter** | Facility writes its own API to a not-yet-integrated program | Escape hatch for any custom agent tool |
| **Data Export / Analytics Adapter** | Rule-based push of events to an external DB | Stream events to your agent datastore / audit log |
| **Nurse-call adapters** | Rauland Responder 5 (via **SIP Adapter**, carries room/bed # + alert type), Hill-Rom NaviCare, Austco Tacera | Route to a room; receive nurse-call events |
| **Monitor adapters** | GE Carescape (broadcast protocol), Nihon Kohden, Spacelabs (REST/SSE), Sotera | Read vitals / alarms |
| **Bed adapters** | **Stryker iBed** (ProCuity → Engage), EarlySense | Read bed telemetry |
| **On-call / staffing** | AMiON, QGenda, Spok, Lightning Bolt, Shift Admin, Amtelco; Rauland ResponderSync; Hillrom Clinical Staff API (REST) | Resolve role → person; assignments |
| **CAP (Common Alerting Protocol)** | XML format for public warnings/emergencies | Mass/emergency notification |
| **LDAP / SAML** | Directory auth / SSO | Identity for the agent |

**Standards to name-drop (credibility):** nurse-call equipment is governed by **UL 1069**;
Hill-Rom integration is via the named **HR Clinical API r13** contract; CMS mandates sepsis
reporting via **Core Measure SEP-1** (which follows the 3- and 6-hour SSC bundles).

**Honest limits (state these to Stryker):**
- Vocera exposes **adapters + a Clinical API + a REST Patient Context service + a Scripted
  Adapter** — but **not** an open third-party developer **FHIR** portal for autonomous agents.
  The realistic architecture calls **Engage's** documented interfaces (or a Scripted Adapter).
- Anything that **autonomously suppresses/deprioritizes clinical alarms** touches
  **510(k)-cleared EMDAN** → frame as **human-in-the-loop augmentation**.
- No public field-level schemas for ProCuity/monitors; no Stryker-native RTLS; no evidence
  Power-LOAD/Power-PRO cots emit clinical telemetry → **speculative, confirm with Stryker**.

## Part D — Sepsis (the best "walk me through it" flow)

A defined, time-critical, nurse-driven protocol → ideal for a multi-agent voice demo.

**Screening ("suspicion of sepsis") — the trigger.** Two standard bedside screens:
- **SIRS** (sensitive): ≥2 of — temp >38.3 °C or <36 °C, HR >90, RR >20, WBC >12k or <4k
  (+ altered mentation) — **plus** suspected/known new infection = sepsis.
- **qSOFA** (Sepsis-3, no labs, specific): ≥2 of — RR ≥22, SBP ≤100 mmHg, altered mental
  status. Designed to flag high-risk infected patients **outside the ICU**.

*Agent implication:* a screening tool reads vitals (RR, SBP, temp, HR) from monitor adapters +
mentation from the nurse's voice, computes SIRS/qSOFA, and flags "suspicion of sepsis" → launch
the bundle walkthrough.

**Surviving Sepsis Campaign Hour-1 Bundle (5 elements).** Begin immediately even if some steps
take longer than an hour:
1. **Measure lactate** (remeasure if initial > 2 mmol/L).
2. **Blood cultures before antibiotics** (don't delay antibiotics if cultures are hard to get).
3. **Broad-spectrum antibiotics.**
4. **Rapid crystalloid** — 30 mL/kg for hypotension or lactate ≥ 4 mmol/L.
5. **Vasopressors** — if hypotensive during/after fluids, to maintain **MAP ≥ 65 mmHg**.

**Ideal voice-agent script:** on a positive screen the agent (a) confirms suspicion of
infection, (b) reads back the 5 steps as a checklist, (c) places lactate + blood-culture
orders (HL7), (d) pages provider/RRT by role, (e) starts a timer and tracks completion against
the hour-1 window, (f) prompts re-measure of lactate if > 2. That maps to **four concurrent
agents**: *screening*, *orders*, *comms/escalation*, and *timer/compliance* — exactly the
parallel, latency-aware orchestration you want to show.

> Clinical currency: the 5 bundle elements are the 2018 SSC hour-1 update (reproduced in a
> peer-reviewed nursing article). The elements remain the standard bedside actions; confirm
> against the current **SSC 2021** guideline before any patient-facing clinical use.

Sources: [SSC hour-1 bundle / Schorr, *American Nurse Journal*](https://www.myamericannurse.com/wp-content/uploads/2018/08/ant9-Sepsis-822a.pdf),
[SIRS/qSOFA (BCEHS)](https://handbook.bcehs.ca/clinical-resources/clinical-scores/sirs-qsofa-sepsis-scores/),
[Vocera Adapters catalog](https://voceradocs.stryker.com/),
[UL 1069](https://www.shopulstandards.com/ProductDetail.aspx?productId=UL1069),
[Vocera Hill-Rom / HR Clinical API r13](https://pubs.vocera.com/vcts/vcts_2.6.0/help/vcts_config_help/topics/vcts_concept_hillrom_connection.html).
