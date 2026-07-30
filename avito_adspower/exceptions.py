class AvitoAdsPowerError(Exception):
    """Base parser error."""


class ConfigurationError(AvitoAdsPowerError):
    pass


class AdsPowerUnavailableError(AvitoAdsPowerError):
    pass


class AdsPowerProfileNotFoundError(AvitoAdsPowerError):
    pass


class AdsPowerProfileStartError(AvitoAdsPowerError):
    pass


class AdsPowerInvalidResponseError(AvitoAdsPowerError):
    pass


class BrowserConnectionError(AvitoAdsPowerError):
    pass


class ProfileBusyError(AvitoAdsPowerError):
    pass


class AvitoAuthenticationRequiredError(AvitoAdsPowerError):
    pass


class AvitoCaptchaError(AvitoAdsPowerError):
    pass


class AvitoRestrictionError(AvitoAdsPowerError):
    pass


class ParserChangedError(AvitoAdsPowerError):
    pass


class SearchDeadlineExceededError(AvitoAdsPowerError):
    pass
