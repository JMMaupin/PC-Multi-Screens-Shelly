"""Tk windows of the application.

Importing the package installs the work-around for Tk bug 7f05a19378 (see
`tk_destroy`) before any window can be created.
"""

from . import tk_destroy

tk_destroy.install()
