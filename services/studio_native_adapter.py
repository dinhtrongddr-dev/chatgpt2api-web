"""Strict opt-in path: one backend, no legacy account retry/cache, real native facts.
Imports of account runtime occur only when an authorized deployment enables it.
"""
from services.studio_receipts import NativeEvent

def native_text_events(body,cancel):
    from services.protocol.conversation import text_backend,conversation_events
    backend=text_backend(model=body['model'])
    backend.transport_request_id=body['studio']['logicalId']
    backend.studio_receipts_strict=True
    terminal=False;done=False;events=None
    try:
        yield NativeEvent('submitted')
        events=conversation_events(backend,messages=body['messages'],model=body['model'],thinking_effort=body.get('reasoning_effort',''))
        for event in events:
            # Cancel is advisory; an unacknowledged cancellation can never produce success.
            if cancel.is_set():raise ValueError('NATIVE_CANCEL_UNVERIFIED')
            if event.get('blocked') or event.get('tool_invoked') or event.get('_image_failure'):yield NativeEvent('refusal');return
            role=event.get('message_role')
            if role=='assistant' and event.get('message_end_turn') is True and event.get('message_status')=='finished_successfully':terminal=True
            if event.get('type')=='conversation.delta':
                if role!='assistant':yield NativeEvent('tool');return
                yield NativeEvent('visible',event.get('delta',''))
            if event.get('type')=='conversation.done':done=True
        # stop is an explicit mapping, not a fabricated upstream finish_reason/usage.
        yield NativeEvent('terminal',proof={'upstreamDone':done,'assistantTerminal':terminal,'finishReason':'stop' if done and terminal else 'unknown','nativeFinishReason':None,'finishReasonProvenance':'native_message_status_and_upstream_done','origin':'native_web_transport'})
    finally:
        try:
            close=getattr(events,'close',None)
            if callable(close):close()
        finally:backend.close()
