"""Register the scoped writer envelope format candidate."""

from .writer_format import VideoFactoryWriterEnvelopeFormatStatus, install

INSTALLATION_STATUS = install()
NODE_CLASS_MAPPINGS = {"VideoFactoryWriterEnvelopeFormatStatus": VideoFactoryWriterEnvelopeFormatStatus}
NODE_DISPLAY_NAME_MAPPINGS = {"VideoFactoryWriterEnvelopeFormatStatus": "VideoFactory · Writer envelope format status"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "INSTALLATION_STATUS"]
