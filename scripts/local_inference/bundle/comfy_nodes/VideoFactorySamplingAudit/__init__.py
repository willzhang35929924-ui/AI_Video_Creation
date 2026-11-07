"""Project-owned, opt-in sampling evidence; imports no models or services."""
from .smd_audit import VideoFactorySMDSamplingAudit,VideoFactoryBaselineSamplingObserver

NODE_CLASS_MAPPINGS={'VideoFactorySMDSamplingAudit':VideoFactorySMDSamplingAudit,
                     'VideoFactoryBaselineSamplingObserver':VideoFactoryBaselineSamplingObserver}
NODE_DISPLAY_NAME_MAPPINGS={'VideoFactorySMDSamplingAudit':'VideoFactory · SMD actual application audit',
                            'VideoFactoryBaselineSamplingObserver':'VideoFactory · Baseline sampling observer'}
__all__=['NODE_CLASS_MAPPINGS','NODE_DISPLAY_NAME_MAPPINGS']
