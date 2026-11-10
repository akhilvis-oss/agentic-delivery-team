# Roadmap

> The plan for how the product gets delivered. The product agent keeps it current; the owner approves changes to order, scope, milestones and weights, recorded in the decision log. Stories reference the section and milestone they belong to, and the stand-up reports progress from it.

## Sections
Group the roadmap into a few sections, each a coherent area of the product. Every story belongs to exactly one section.

| ID | Section | Outcome when complete | Priority | Status |
| --- | --- | --- | --- | --- |
| S1 | <Area, e.g. Onboarding> | <What users can do when this section is done> | 1 | <Planned / In progress / Done> |
| S2 | <Area> | <Outcome> | 2 | <Planned> |

## Milestones
A milestone is a usable release that cuts across sections. Define the first one before writing stories.

### M1: <Name>
- **Done means:** <What a user can do end to end when M1 is complete>
- **Target:** <date>
- **Stories and weights:** each story's weight is its share of M1. Weights add up to 100%. Completion is the sum of the weights of stories that are done.

| Story | Section | Weight | Status |
| --- | --- | --- | --- |
| <#issue> | S1 | <30%> | <Planned> |
| <#issue> | S2 | <20%> | <Planned> |

## Critical regression set
A small set of tests that protects what must never break. It runs on every story and should take minutes, not hours.

- <Test or suite name>: <what it protects>
- <Test or suite name>: <what it protects>

## Later
- <Ideas not yet prioritised>

## Not doing
- <Ideas considered and rejected, with a link to the decision>
