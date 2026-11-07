"""Install the candidate reference-unit policy on the pinned H3 analyser."""

from .cache_policy import VideoFactoryReferenceCachePolicyStatus, install

INSTALLATION_STATUS = install()
NODE_CLASS_MAPPINGS = {"VideoFactoryReferenceCachePolicyStatus": VideoFactoryReferenceCachePolicyStatus}
NODE_DISPLAY_NAME_MAPPINGS = {"VideoFactoryReferenceCachePolicyStatus": "VideoFactory · Reference cache policy status"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "INSTALLATION_STATUS"]
