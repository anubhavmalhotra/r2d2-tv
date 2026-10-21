"""Map an isolated provider's fixed failures into this controller's own type.

A rendered voice_control package and a source backend worker can coexist without
sharing Python class identities. This wrapper creates no authority or transport.
"""
if __package__:
    from .controller import Refused
else:
    from controller import Refused


class LocalIntentAdapter:
    def __init__(self, provider, provider_refused):
        if (not callable(getattr(provider, 'interpret', None)) or not isinstance(provider_refused, type) or
                not issubclass(provider_refused, Exception) or provider_refused is Exception):
            raise ValueError('MODEL_ADAPTER_CONFIG')
        self.provider, self.provider_refused = provider, provider_refused

    @property
    def prompt_sha256(self):
        return getattr(self.provider, 'prompt_sha256', None)

    def configure_prompt(self, system_prompt, *, phase=None):
        fn = getattr(self.provider, 'configure_prompt', None)
        if not callable(fn):
            raise Refused('MODEL_CONFIG')
        try:
            return fn(system_prompt, phase=phase)
        except Exception as error:
            code = str(error) if type(error) is self.provider_refused else None
            raise Refused(code if code in ('MODEL_CONFIG', 'MODEL_UNAVAILABLE', 'MODEL_SCHEMA')
                          else 'MODEL_CONFIG') from None

    def interpret(self, text, *, timeout_s):
        try:
            return self.provider.interpret(text, timeout_s=timeout_s)
        except Exception as error:
            code = str(error) if type(error) is self.provider_refused else None
            raise Refused(code if code in ('MODEL_CONFIG', 'MODEL_UNAVAILABLE', 'MODEL_SCHEMA')
                          else 'MODEL_UNAVAILABLE') from None
