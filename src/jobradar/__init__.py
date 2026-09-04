"""jobradar -- an unattended remote-job data pipeline.

Scrapes three sources on a schedule, validates and stores what it finds, and
serves a dashboard over the result. Designed so that the interesting questions
have answers: what happens when a source changes its HTML, when the network
drops halfway through, and when the whole thing runs twice by accident.
"""

__version__ = "1.0.0"
