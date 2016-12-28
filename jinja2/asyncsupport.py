# -*- coding: utf-8 -*-
"""
    jinja2.asyncsupport
    ~~~~~~~~~~~~~~~~~~~

    Has all the code for async support which is implemented as a patch
    for supported Python versions.

    :copyright: (c) 2017 by the Jinja Team.
    :license: BSD, see LICENSE for more details.
"""
import sys
import asyncio
import inspect
from functools import update_wrapper

from jinja2.utils import concat, internalcode, concat, Markup
from jinja2.environment import TemplateModule
from jinja2.runtime import LoopContextBase, _last_iteration


async def concat_async(async_gen):
    rv = []
    async def collect():
        async for event in async_gen:
            rv.append(event)
    await collect()
    return concat(rv)


async def generate_async(self, *args, **kwargs):
    vars = dict(*args, **kwargs)
    try:
        async for event in self.root_render_func(self.new_context(vars)):
            yield event
    except Exception:
        exc_info = sys.exc_info()
    else:
        return
    yield self.environment.handle_exception(exc_info, True)


def wrap_generate_func(original_generate):
    def _convert_generator(self, loop, args, kwargs):
        async_gen = self.generate_async(*args, **kwargs)
        try:
            while 1:
                yield loop.run_until_complete(async_gen.__anext__())
        except StopAsyncIteration:
            pass
    def generate(self, *args, **kwargs):
        if not self.environment._async:
            return original_generate(self, *args, **kwargs)
        return _convert_generator(self, asyncio.get_event_loop(), args, kwargs)
    return update_wrapper(generate, original_generate)


async def render_async(self, *args, **kwargs):
    if not self.environment._async:
        raise RuntimeError('The environment was not created with async mode '
                           'enabled.')

    vars = dict(*args, **kwargs)
    ctx = self.new_context(vars)

    try:
        return await concat_async(self.root_render_func(ctx))
    except Exception:
        exc_info = sys.exc_info()
    return self.environment.handle_exception(exc_info, True)


def wrap_render_func(original_render):
    def render(self, *args, **kwargs):
        if not self.environment._async:
            return original_render(self, *args, **kwargs)
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.render_async(*args, **kwargs))
    return update_wrapper(render, original_render)


def wrap_block_reference_call(original_call):
    @internalcode
    async def async_call(self):
        rv = await concat_async(self._stack[self._depth](self._context))
        if self._context.eval_ctx.autoescape:
            rv = Markup(rv)
        return rv

    @internalcode
    def __call__(self):
        if not self._context.environment._async:
            return original_call(self)
        return async_call(self)

    return update_wrapper(__call__, original_call)


@internalcode
async def get_default_module_async(self):
    if self._module is not None:
        return self._module
    self._module = rv = await self.make_module_async()
    return rv


def wrap_default_module(original_default_module):
    @internalcode
    def _get_default_module(self):
        if self.environment._async:
            raise RuntimeError('Template module attribute is unavailable '
                               'in async mode')
        return original_default_module(self)
    return _get_default_module


async def make_module_async(self, vars=None, shared=False, locals=None):
    context = self.new_context(vars, shared, locals)
    body_stream = []
    async for item in self.root_render_func(context):
        body_stream.append(item)
    return TemplateModule(self, context, body_stream)


def patch_template():
    from jinja2 import Template
    Template.generate = wrap_generate_func(Template.generate)
    Template.generate_async = update_wrapper(
        generate_async, Template.generate_async)
    Template.render_async = update_wrapper(
        render_async, Template.render_async)
    Template.render = wrap_render_func(Template.render)
    Template._get_default_module = wrap_default_module(
        Template._get_default_module)
    Template._get_default_module_async = get_default_module_async
    Template.make_module_async = update_wrapper(
        make_module_async, Template.make_module_async)


def patch_runtime():
    from jinja2.runtime import BlockReference
    BlockReference.__call__ = wrap_block_reference_call(BlockReference.__call__)


def patch_all():
    patch_template()
    patch_runtime()


async def auto_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def auto_iter(iterable):
    if hasattr(iterable, '__aiter__'):
        async for item in iterable:
            yield item
        return
    for item in iterable:
        yield item


class AsyncLoopContext(LoopContextBase):

    def __init__(self, async_iterator, iterable, after, recurse=None, depth0=0):
        self._async_iterator = async_iterator
        LoopContextBase.__init__(self, iterable, recurse, depth0)
        self._after = after

    def __aiter__(self):
        return AsyncLoopContextIterator(self)


class AsyncLoopContextIterator(object):
    __slots__ = ('context',)

    def __init__(self, context):
        self.context = context

    def __aiter__(self):
        return self

    async def __anext__(self):
        ctx = self.context
        ctx.index0 += 1
        if ctx._after is _last_iteration:
            raise StopAsyncIteration()
        next_elem = ctx._after
        try:
            ctx._after = await ctx._async_iterator.__anext__()
        except StopAsyncIteration:
            ctx._after = _last_iteration
        return next_elem, ctx


async def make_async_loop_context(iterable, recurse=None, depth0=0):
    async_iterator = auto_iter(iterable)
    try:
        after = await async_iterator.__anext__()
    except StopAsyncIteration:
        after = _last_iteration
    return AsyncLoopContext(async_iterator, iterable, after, recurse, depth0)