"""VAT oracle (Part 1): deterministic rule engine.

Public modules:
    rules      - bounded rule set, tables, the five subtask resolvers
    generator  - seeded synthetic case generation (the only RNG user)
    labeler    - compose the resolvers over a whole case -> oracle trace
    validator  - the V checks over any emitted trace
    scorer     - compare an emitted trace vs oracle labels
"""
