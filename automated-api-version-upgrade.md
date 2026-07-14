# Technical Design Requirements: Automated AzureRM API Version Upgrade

## 1. Background & Scope

Upgrading API versions in AzureRM is largely mechanical but error-prone. Historically, some upgrades introduced breaking changes that weren't caught in advance, forcing reactive downgrades. This initiative aims to **automate the upgrade flow** and **catch breaking changes earlier**.

> **Positioning:** AzureRM has historically not pursued new API versions proactively unless driven by a feature request. That guidance is now considered grey-area, so this work should be framed as improving upgrade **safety and efficiency** — not mass-upgrading for its own sake.

---

## 2. Functional Requirements

### 2.1 Core upgrade automation

- Bump the API version consumed by AzureRM for a given RP (assumes Pandora and the Go Azure SDK are already updated — see [§3](#3-pre-upgrade-dependencies-must-not-be-overlooked)).
- Apply the corresponding implementation and documentation changes.
- Run the relevant acceptance tests and iterate on failures.

### 2.2 Cross-API breaking-change detection *(required)*

Integrate the cross-API drift-detection approach previously prototyped by Heng:

1. Provision a resource using **AzureRM v1 + API v1**.
2. Re-plan / refresh the same resource using **AzureRM v2 + API v2**.
3. Detect whether any Terraform drift is produced between the two versions.
4. Surface detected drift as a breaking-change signal.

### 2.3 AI breaking-change analysis

- Analyze Learn docs and REST API specs to identify documented breaking changes between versions.
- Combine this with the drift-detection output from [§2.2](#22-cross-api-breaking-change-detection-required) to produce a **consolidated breaking-change report**.

### 2.4 Test comprehensiveness

- Testing must be thorough enough to catch breaking changes **before merge** — the historical failure mode is undetected breakage leading to downgrades.

---

## 3. Pre-Upgrade Dependencies *(must not be overlooked)*

The actual API bump in AzureRM is the **last** step. For this to be usable day-to-day, the preceding steps need to be accounted for:

| Step | Action | Note |
| ---- | ------ | ---- |
| 1 | Add / define the new API version in the Pandora definitions repo. | Sometimes, existing API version has data workaround. The new API version might need to be added there |
| 2 | Generate a new HashiCorp Go Azure SDK from those definitions. |
| 3 | Vendor / introduce that SDK into AzureRM. |
| 4 | Perform the API version bump and code/doc changes. |

> A design that only covers **Step 4** will be discounted in practice. These pre-steps should be in scope for the long-term design, even if not fully automated in the PoC.

---

## 4. Non-Functional Requirements / Constraints

- **No automatic PR submission.** Regardless of experiment outcome, do not submit PRs directly to the AzureRM repo. Human review is required.
- **HashiCorp review is the bottleneck.** If this generates dozens of API-bump PRs, they will queue on HashiCorp review. Volume of output is *not* the success metric.
- **Target a process, not a one-off tool.** The end state should be a repeatable process/mechanism integrated into the team workflow, not a standalone tool each developer triggers manually.
  - If a fully automated process proves too hard and we land on a manually-triggered tool, that is still an acceptable and valuable outcome worth spreading across the team.

---

## 5. PoC Plan

Run a pilot on **3–5 representative RPs** to evaluate:

- The automation process end to end.
- Output quality vs. the current manual engineering approach.

### Open design decision *(to be made during PoC)*

Choose between two execution models:

1. **AI designs the process + tooling**, then execute it via a conventional (non-AI) engineering implementation, **or**
2. **AI executes the upgrade end to end.**

---

## 6. Success Criteria

- [ ] Breaking changes are detected **before merge** (no post-merge downgrades).
- [ ] Automated output quality is comparable to or better than the manual process.
- [ ] A clear path exists from PoC toward an integrated, repeatable team process.
