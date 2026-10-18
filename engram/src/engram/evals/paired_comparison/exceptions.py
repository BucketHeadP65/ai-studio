"""The errors a caller of the paired comparison handles."""


class ResultsFileUnreadableError(Exception):
    """A results file cannot be read as one evaluation run's results.

    The file is missing or empty, a line is not a header or a case line, or a line repeats a case. The message names
    the line.
    """


class RunsNotComparableError(ValueError):
    """Two evaluation runs cannot be compared case by case.

    Their variants differ in a held field or in both the SQL switch and code mode, their strategies make a pair the graph
    verdict calls unfair, a case is in one run only, or they hold no cases. The message names the field, gives the
    verdict's reason or names the case.
    """
