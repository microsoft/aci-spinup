class AciSpinupError(RuntimeError):
    """Base class for expected command failures."""


class DeploymentError(AciSpinupError):
    """Raised when deployment cannot finish its post-deploy work."""


class InventoryError(AciSpinupError):
    """Raised when deletion inventory cannot be completed."""


class UnsafeDeletionError(AciSpinupError):
    def __init__(self, unexpected_resource_ids: list[str]):
        self.unexpected_resource_ids = unexpected_resource_ids
        resources = "\n".join(
            f"  {resource_id}" for resource_id in unexpected_resource_ids
        )
        super().__init__(
            "Refusing to delete the resource group because it contains "
            f"unexpected top-level ARM resources:\n{resources}"
        )
