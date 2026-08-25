"""One adapter per graph database, all implementing GraphDBAdapter (see base.py).

Why an adapter pattern: it's the only way to guarantee we're asking every database
the *same logical question* through each engine's native driver, instead of writing
five bespoke scripts that quietly drift apart. The harness only ever calls the
abstract interface — it never knows which concrete DB it's talking to.
"""
