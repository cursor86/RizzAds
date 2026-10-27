class AdEngineError(Exception):
    """Base class for all domain errors raised by AdEngine-AI."""


class DeployValidationError(AdEngineError):
    """A launch request is missing approved creative or has invalid settings. Nothing was created."""


class MediaProcessingError(AdEngineError):
    """A media upload was rejected or did not finish processing on the platform."""


class ExternalAPIError(AdEngineError):
    """A platform API rejected a request after retries were exhausted."""


class LLMOutputError(AdEngineError):
    """The LLM returned output that does not match the required schema."""
