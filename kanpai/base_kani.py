from contextlib import contextmanager
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from kani import ChatMessage, ChatRole, Kani
from kani.engines.base import Completion

from . import events, prompts
from .state import KaniState, RunState
from .utils import create_kani_id

if TYPE_CHECKING:
    from .app import Kanpai


class BaseKani(Kani):
    def __init__(self, *args, app: "Kanpai", parent: "BaseKani" = None, id: str = None, name: str = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = RunState.STOPPED
        # tree management
        if parent is not None:
            self.depth = parent.depth + 1
        else:
            self.depth = 0
        self.parent = parent
        self.children = WeakValueDictionary()
        # app management
        self.id = create_kani_id() if id is None else id
        self.name = self.id if name is None else name
        self.app = app
        app.on_kani_creation(self)

    # ==== overrides ====
    async def chat_round(self, *args, **kwargs):
        with self.set_state(RunState.RUNNING):
            return await super().chat_round(*args, **kwargs)

    async def full_round(self, *args, **kwargs):
        self.always_included_messages[0] = ChatMessage.system(prompts.get_system_prompt(self))
        with self.set_state(RunState.RUNNING):
            async for msg in super().full_round(*args, **kwargs):
                yield msg

    async def add_to_history(self, message: ChatMessage):
        await super().add_to_history(message)
        self.app.dispatch(events.KaniMessage(id=self.id, msg=message))

    async def get_model_completion(self, include_functions: bool = True, **kwargs):
        completion = await super().get_model_completion(include_functions, **kwargs)
        self.app.dispatch(
            events.TokensUsed(
                id=self.id, prompt_tokens=completion.prompt_tokens, completion_tokens=completion.completion_tokens
            )
        )
        message = completion.message
        # HACK: sometimes openai's function calls are borked; we fix them here
        if (function_call := message.function_call) and function_call.name.startswith("functions."):
            fixed_name = function_call.name.removeprefix("functions.")
            message = message.copy_with(function_call=function_call.copy_with(name=fixed_name))
            return Completion(
                message, prompt_tokens=completion.prompt_tokens, completion_tokens=completion.completion_tokens
            )
        return completion

    # ==== utils ====
    @property
    def last_user_message(self) -> ChatMessage | None:
        return next((m for m in self.chat_history if m.role == ChatRole.USER), None)

    @contextmanager
    def set_state(self, state: RunState):
        """Run the body of this statement with a different run state then set it back after."""
        old_state = self.state
        self.state = state
        self.app.dispatch(events.KaniStateChange(id=self.id, state=self.state))
        try:
            yield
        finally:
            if self.state != RunState.ERRORED:
                self.state = old_state
                self.app.dispatch(events.KaniStateChange(id=self.id, state=self.state))

    def get_save_state(self) -> KaniState:
        """Get a Pydantic state suitable for saving/loading."""
        return KaniState(
            id=self.id,
            depth=self.depth,
            parent=self.parent.id if self.parent else None,
            children=list(self.children),
            always_included_messages=self.always_included_messages,
            chat_history=self.chat_history,
            state=self.state,
            name=self.name,
        )

    async def cleanup(self):
        """This kani may run again but is done for now; clean up any ephemeral resources but save its state."""
        pass
