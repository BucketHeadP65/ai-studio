"""The wireup registrations, one module per slice the application composes.

Each module binds one slice's adapters and handlers to the container under the types their consumers resolve. The composition root
assembles them all, and a slice that consumes another's port never learns how that port is built. The `contracts` module holds the
types that modules outside this package import.
"""
