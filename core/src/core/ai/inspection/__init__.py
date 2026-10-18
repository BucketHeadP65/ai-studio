"""Inspection: how a run's transcript reaches the rules and the inspector and how what they find gets out.

The taxonomy says what can go wrong and what is to blame. A rule reads a transcript and produces leads, the inspector
turns a lead into a finding, an inspection is the whole pass over one run, a watch says which runs get which rules and
whether the inspector, and the `InspectRun` handler starts one inspection over one run.
"""
