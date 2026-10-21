"""Manager-side push of the closed voice receipt card to its own Trial viewer.

Runs on the existing ResidentEntry service loop: no thread, listener, retry or
log. The card travels only over the inherited signed Manager/Trial channel.
"""
if __package__:
    from .browser_channel import valid_card
else:
    from browser_channel import valid_card

# A timed-out exchange closes the channel browser navigation shares, so allow a
# brief Trial stall rather than failing fast. Lock wait stays at 50 ms.
EXCHANGE_S = 1.5


def push_voice_card(manager, voice_status, timeout_s=.25):
    """Best effort and silent; returns whether the Trial mirror took the card.

    The card is validated before any request, so a malformed card can never
    poison the channel that browser navigation depends on.
    """
    try:
        spawn = getattr(manager, 'control_spawn', None)
        read = getattr(spawn, 'current_client', None)
        binding = read(timeout_s=timeout_s) if callable(read) else None
        if type(binding) is not tuple or len(binding) != 2:
            return False
        client = binding[1]
        push = getattr(client, 'voice_card', None)
        if not callable(push) or getattr(client, 'session', None) != binding[0]:
            return False
        card = voice_status.viewer_card()
        card = dict(card) if type(card) is dict else card
        if card is not None and not valid_card(card):
            return False
        result = push(card, timeout_s=EXCHANGE_S)
        if type(result) is not dict or set(result) != {'delivered'}:
            return False
        if result['delivered'] is not None:
            voice_status.note_viewer_delivered(result['delivered'])
        return True
    except Exception:
        return False
