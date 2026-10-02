---
name: bayesian-knowledge-tracing
description: Solves latent mastery transitions and traces the continuous prerequisite learning frontier.
license: MIT
allowed-tools: ""
metadata:
  author: "Rucha Salpure"
  version: "1.0.0"
  category: learner-modeling
---

# Bayesian Knowledge Tracing Skill

## Instructions
1. Maintain continuous latent mastery probabilities P(L) for every concept in the subject graph.
2. Update mastery state upon each evidence event using standard slip and guess parameter priors.
3. Identify the active learning frontier: concepts whose prerequisites are satisfied but whose mastery remains in development.
4. Pass updated state distributions to the lead NavigatorAgent to guide the next instructional move.
