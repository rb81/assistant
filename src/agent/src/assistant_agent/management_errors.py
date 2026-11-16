PERMISSION_ACTIVATION_ERROR = (
    "Permissions were saved, but active sandbox runs could not be stopped. "
    "Stop the sandbox service before relying on revocation, then check current permissions and service health."
)


class ApprovalApplicationIncomplete(RuntimeError):
    """The one-use approval was consumed but its external action did not finish."""

    MESSAGE = (
        "The approval was consumed, but applying the action failed; the action may have partially occurred. "
        "Verify its status before taking further action."
    )

    def __init__(self):
        super().__init__(self.MESSAGE)
