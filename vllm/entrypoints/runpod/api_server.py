# Adapted from
# https://github.com/lm-sys/FastChat/blob/168ccc29d3f7edc50823016105c024fe2282732a/fastchat/serve/openai_api_server.py

import argparse
import asyncio
from http import HTTPStatus
import json
import time
from typing import AsyncGenerator, Dict, List, Optional

import fastapi
from fastapi import BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastchat.conversation import Conversation, SeparatorStyle
from fastchat.model.model_adapter import get_conversation_template

import uvicorn
from vllm.config import ModelConfig

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.entrypoints.openai.protocol import (
    CompletionRequest, CompletionResponse, CompletionResponseChoice,
    CompletionResponseStreamChoice, CompletionStreamResponse,
    ChatCompletionRequest, ChatCompletionResponse,
    ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse, ChatMessage, DeltaMessage, ErrorResponse,
    LogProbs, ModelCard, ModelList, ModelPermission, UsageInfo)
from vllm.logger import init_logger
from vllm.outputs import RequestOutput
from vllm.sampling_params import SamplingParams
from vllm.transformers_utils.tokenizer import get_tokenizer
from vllm.utils import random_uuid

class RunpodVLLM():
    __name__ = 'RunpodVLLM'

    def __init__(self, host: str, port: int, tokenizer, engine, engine_model_config, served_model, uvicorn_timeout=5):
        # Prepare instance variables
        self.logger = init_logger(__name__)
        self.tokenizer = tokenizer
        self.engine: AsyncLLMEngine = engine
        self.engine_model_config: ModelConfig = engine_model_config
        self.served_model: str = served_model
        self.uvicorn_timeout: int = uvicorn_timeout

        # Prepare the middleware
        self.app = fastapi.FastAPI()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"]
        )

        # Setup uvicorn params
        self.host = host
        self.port = port
        self.uvicorn_timeout = uvicorn_timeout

        # Configure the fastapi routes
        self.configure_routes()

    def start(self):
        # Start the uvicorn instance.
        uvicorn.run(self.app,
                    host=self.host,
                    port=self.port,
                    log_level="info",
                    timeout_keep_alive=self.uvicorn_timeout,)


    # Configure the FastAPI Routes
    def configure_routes(self):
        def create_error_response(status_code: HTTPStatus,
                                message: str) -> JSONResponse:
            return JSONResponse(ErrorResponse(message=message,
                                            type="invalid_request_error").dict(),
                                status_code=status_code.value)

        @self.app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request, exc):  # pylint: disable=unused-argument
            return create_error_response(HTTPStatus.BAD_REQUEST, str(exc))


        async def check_model(request) -> Optional[JSONResponse]:
            if request.model == self.served_model:
                return
            ret = create_error_response(
                HTTPStatus.NOT_FOUND,
                f"The model `{request.model}` does not exist.",
            )
            return ret


        async def get_gen_prompt(request) -> str:
            conv = get_conversation_template(request.model)
            conv = Conversation(
                name=conv.name,
                system=conv.system,
                roles=conv.roles,
                messages=list(conv.messages),  # prevent in-place modification
                offset=conv.offset,
                sep_style=SeparatorStyle(conv.sep_style),
                sep=conv.sep,
                sep2=conv.sep2,
                stop_str=conv.stop_str,
                stop_token_ids=conv.stop_token_ids,
            )

            if isinstance(request.messages, str):
                prompt = request.messages
            else:
                for message in request.messages:
                    msg_role = message["role"]
                    if msg_role == "system":
                        conv.system = message["content"]
                    elif msg_role == "user":
                        conv.append_message(conv.roles[0], message["content"])
                    elif msg_role == "assistant":
                        conv.append_message(conv.roles[1], message["content"])
                    else:
                        raise ValueError(f"Unknown role: {msg_role}")

                # Add a blank message for the assistant.
                conv.append_message(conv.roles[1], None)
                prompt = conv.get_prompt()

            return prompt


        async def check_length(request, prompt, model_config):
            if hasattr(model_config.hf_config, "max_sequence_length"):
                context_len = model_config.hf_config.max_sequence_length
            elif hasattr(model_config.hf_config, "seq_length"):
                context_len = model_config.hf_config.seq_length
            elif hasattr(model_config.hf_config, "max_position_embeddings"):
                context_len = model_config.hf_config.max_position_embeddings
            elif hasattr(model_config.hf_config, "seq_length"):
                context_len = model_config.hf_config.seq_length
            else:
                context_len = 2048

            input_ids = self.tokenizer(prompt).input_ids
            token_num = len(input_ids)

            if token_num + request.max_tokens > context_len:
                return create_error_response(
                    HTTPStatus.BAD_REQUEST,
                    f"This model's maximum context length is {context_len} tokens. "
                    f"However, you requested {request.max_tokens + token_num} tokens "
                    f"({token_num} in the messages, "
                    f"{request.max_tokens} in the completion). "
                    f"Please reduce the length of the messages or completion.",
                )
            else:
                return None


        @self.app.get("/v1/models")
        async def show_available_models():
            """Show available models. Right now we only have one model."""
            model_cards = [
                ModelCard(id=self.served_model,
                        root=self.served_model,
                        permission=[ModelPermission()])
            ]
            return ModelList(data=model_cards)


        def create_logprobs(token_ids: List[int],
                            id_logprobs: List[Dict[int, float]],
                            initial_text_offset: int = 0) -> LogProbs:
            """Create OpenAI-style logprobs."""
            logprobs = LogProbs()
            last_token_len = 0
            for token_id, id_logprob in zip(token_ids, id_logprobs):
                token = self.tokenizer.convert_ids_to_tokens(token_id)
                logprobs.tokens.append(token)
                logprobs.token_logprobs.append(id_logprob[token_id])
                if len(logprobs.text_offset) == 0:
                    logprobs.text_offset.append(initial_text_offset)
                else:
                    logprobs.text_offset.append(logprobs.text_offset[-1] +
                                                last_token_len)
                last_token_len = len(token)

                logprobs.top_logprobs.append({
                    self.tokenizer.convert_ids_to_tokens(i): p
                    for i, p in id_logprob.items()
                })
            return logprobs


        @self.app.post("/v1/chat/completions")
        async def create_chat_completion(raw_request: Request):
            """Completion API similar to OpenAI's API.

            See  https://platform.openai.com/docs/api-reference/chat/create
            for the API specification. This API mimics the OpenAI ChatCompletion API.

            NOTE: Currently we do not support the following features:
                - function_call (Users should implement this by themselves)
                - logit_bias (to be supported by vLLM engine)
            """
            request = ChatCompletionRequest(**await raw_request.json())
            self.logger.info(f"Received chat completion request: {request}")

            error_check_ret = await check_model(request)
            if error_check_ret is not None:
                return error_check_ret

            if request.logit_bias is not None:
                # TODO: support logit_bias in vLLM engine.
                return create_error_response(HTTPStatus.BAD_REQUEST,
                                            "logit_bias is not currently supported")

            prompt = await get_gen_prompt(request)
            error_check_ret = await check_length(request, prompt, self.engine_model_config)
            if error_check_ret is not None:
                return error_check_ret

            model_name = request.model
            request_id = f"cmpl-{random_uuid()}"
            created_time = int(time.time())
            try:
                sampling_params = SamplingParams(
                    n=request.n,
                    presence_penalty=request.presence_penalty,
                    frequency_penalty=request.frequency_penalty,
                    temperature=request.temperature,
                    top_p=request.top_p,
                    stop=request.stop,
                    max_tokens=request.max_tokens,
                    best_of=request.best_of,
                    top_k=request.top_k,
                    ignore_eos=request.ignore_eos,
                    use_beam_search=request.use_beam_search,
                )
            except ValueError as e:
                return create_error_response(HTTPStatus.BAD_REQUEST, str(e))

            result_generator = self.engine.generate(prompt, sampling_params, request_id)

            async def abort_request() -> None:
                await self.engine.abort(request_id)

            def create_stream_response_json(
                index: int,
                text: str,
                finish_reason: Optional[str] = None,
            ) -> str:
                choice_data = ChatCompletionResponseStreamChoice(
                    index=index,
                    delta=DeltaMessage(content=text),
                    finish_reason=finish_reason,
                )
                response = ChatCompletionStreamResponse(
                    id=request_id,
                    created=created_time,
                    model=model_name,
                    choices=[choice_data],
                )
                response_json = response.json(ensure_ascii=False)

                return response_json

            async def completion_stream_generator() -> AsyncGenerator[str, None]:
                # First chunk with role
                for i in range(request.n):
                    choice_data = ChatCompletionResponseStreamChoice(
                        index=i,
                        delta=DeltaMessage(role="assistant"),
                        finish_reason=None,
                    )
                    chunk = ChatCompletionStreamResponse(id=request_id,
                                                        choices=[choice_data],
                                                        model=model_name)
                    data = chunk.json(exclude_unset=True, ensure_ascii=False)
                    yield f"data: {data}\n\n"

                previous_texts = [""] * request.n
                previous_num_tokens = [0] * request.n
                async for res in result_generator:
                    res: RequestOutput
                    for output in res.outputs:
                        i = output.index
                        delta_text = output.text[len(previous_texts[i]):]
                        previous_texts[i] = output.text
                        previous_num_tokens[i] = len(output.token_ids)
                        response_json = create_stream_response_json(
                            index=i,
                            text=delta_text,
                        )
                        yield f"data: {response_json}\n\n"
                        if output.finish_reason is not None:
                            response_json = create_stream_response_json(
                                index=i,
                                text="",
                                finish_reason=output.finish_reason,
                            )
                            yield f"data: {response_json}\n\n"
                    yield "data: [DONE]\n\n"

            # Streaming response
            if request.stream:
                background_tasks = BackgroundTasks()
                # Abort the request if the client disconnects.
                background_tasks.add_task(abort_request)
                return StreamingResponse(completion_stream_generator(),
                                        media_type="text/event-stream",
                                        background=background_tasks)

            # Non-streaming response
            final_res: RequestOutput = None
            async for res in result_generator:
                if await raw_request.is_disconnected():
                    # Abort the request if the client disconnects.
                    await abort_request()
                    return create_error_response(HTTPStatus.BAD_REQUEST,
                                                "Client disconnected")
                final_res = res
            assert final_res is not None
            choices = []
            for output in final_res.outputs:
                choice_data = ChatCompletionResponseChoice(
                    index=output.index,
                    message=ChatMessage(role="assistant", content=output.text),
                    finish_reason=output.finish_reason,
                )
                choices.append(choice_data)

            num_prompt_tokens = len(final_res.prompt_token_ids)
            num_generated_tokens = sum(
                len(output.token_ids) for output in final_res.outputs)
            usage = UsageInfo(
                prompt_tokens=num_prompt_tokens,
                completion_tokens=num_generated_tokens,
                total_tokens=num_prompt_tokens + num_generated_tokens,
            )
            response = ChatCompletionResponse(
                id=request_id,
                created=created_time,
                model=model_name,
                choices=choices,
                usage=usage,
            )

            if request.stream:
                # When user requests streaming but we don't stream, we still need to
                # return a streaming response with a single event.
                response_json = response.json(ensure_ascii=False)

                async def fake_stream_generator() -> AsyncGenerator[str, None]:
                    yield f"data: {response_json}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(fake_stream_generator(),
                                        media_type="text/event-stream")

            return response


        @self.app.post("/v1/completions")
        async def create_completion(raw_request: Request):
            """Completion API similar to OpenAI's API.

            See https://platform.openai.com/docs/api-reference/completions/create
            for the API specification. This API mimics the OpenAI Completion API.

            NOTE: Currently we do not support the following features:
                - echo (since the vLLM engine does not currently support
                getting the logprobs of prompt tokens)
                - suffix (the language models we currently support do not support
                suffix)
                - logit_bias (to be supported by vLLM engine)
            """
            request = CompletionRequest(**await raw_request.json())
            self.logger.info(f"Received completion request: {request}")

            error_check_ret = await check_model(request)
            if error_check_ret is not None:
                return error_check_ret

            if request.echo:
                # We do not support echo since the vLLM engine does not
                # currently support getting the logprobs of prompt tokens.
                return create_error_response(HTTPStatus.BAD_REQUEST,
                                            "echo is not currently supported")

            if request.suffix is not None:
                # The language models we currently support do not support suffix.
                return create_error_response(HTTPStatus.BAD_REQUEST,
                                            "suffix is not currently supported")

            if request.logit_bias is not None:
                # TODO: support logit_bias in vLLM engine.
                return create_error_response(HTTPStatus.BAD_REQUEST,
                                            "logit_bias is not currently supported")

            model_name = request.model
            request_id = f"cmpl-{random_uuid()}"
            if isinstance(request.prompt, list):
                if len(request.prompt) == 0:
                    return create_error_response(HTTPStatus.BAD_REQUEST,
                                                "please provide at least one prompt")
                if len(request.prompt) > 1:
                    return create_error_response(
                        HTTPStatus.BAD_REQUEST,
                        "multiple prompts in a batch is not currently supported")
                prompt = request.prompt[0]
            else:
                prompt = request.prompt
            created_time = int(time.time())
            try:
                sampling_params = SamplingParams(
                    n=request.n,
                    best_of=request.best_of,
                    presence_penalty=request.presence_penalty,
                    frequency_penalty=request.frequency_penalty,
                    temperature=request.temperature,
                    top_p=request.top_p,
                    top_k=request.top_k,
                    stop=request.stop,
                    ignore_eos=request.ignore_eos,
                    max_tokens=request.max_tokens,
                    logprobs=request.logprobs,
                    use_beam_search=request.use_beam_search,
                )
            except ValueError as e:
                return create_error_response(HTTPStatus.BAD_REQUEST, str(e))

            result_generator = self.engine.generate(prompt, sampling_params, request_id)

            # Similar to the OpenAI API, when n != best_of, we do not stream the
            # results. In addition, we do not stream the results when use beam search.
            stream = (request.stream
                    and (request.best_of is None or request.n == request.best_of)
                    and not request.use_beam_search)

            async def abort_request() -> None:
                await self.engine.abort(request_id)

            def create_stream_response_json(
                index: int,
                text: str,
                logprobs: Optional[LogProbs] = None,
                finish_reason: Optional[str] = None,
            ) -> str:
                choice_data = CompletionResponseStreamChoice(
                    index=index,
                    text=text,
                    logprobs=logprobs,
                    finish_reason=finish_reason,
                )
                response = CompletionStreamResponse(
                    id=request_id,
                    created=created_time,
                    model=model_name,
                    choices=[choice_data],
                )
                response_json = response.json(ensure_ascii=False)

                return response_json

            async def completion_stream_generator() -> AsyncGenerator[str, None]:
                previous_texts = [""] * request.n
                previous_num_tokens = [0] * request.n
                async for res in result_generator:
                    res: RequestOutput
                    for output in res.outputs:
                        i = output.index
                        delta_text = output.text[len(previous_texts[i]):]
                        if request.logprobs is not None:
                            logprobs = create_logprobs(
                                output.token_ids[previous_num_tokens[i]:],
                                output.logprobs[previous_num_tokens[i]:],
                                len(previous_texts[i]))
                        else:
                            logprobs = None
                        previous_texts[i] = output.text
                        previous_num_tokens[i] = len(output.token_ids)
                        response_json = create_stream_response_json(
                            index=i,
                            text=delta_text,
                            logprobs=logprobs,
                        )
                        yield f"data: {response_json}\n\n"
                        if output.finish_reason is not None:
                            logprobs = (LogProbs()
                                        if request.logprobs is not None else None)
                            response_json = create_stream_response_json(
                                index=i,
                                text="",
                                logprobs=logprobs,
                                finish_reason=output.finish_reason,
                            )
                            yield f"data: {response_json}\n\n"
                    yield "data: [DONE]\n\n"

            # Streaming response
            if stream:
                background_tasks = BackgroundTasks()
                # Abort the request if the client disconnects.
                background_tasks.add_task(abort_request)
                return StreamingResponse(completion_stream_generator(),
                                        media_type="text/event-stream",
                                        background=background_tasks)

            # Non-streaming response
            final_res: RequestOutput = None
            async for res in result_generator:
                if await raw_request.is_disconnected():
                    # Abort the request if the client disconnects.
                    await abort_request()
                    return create_error_response(HTTPStatus.BAD_REQUEST,
                                                "Client disconnected")
                final_res = res
            assert final_res is not None
            choices = []
            for output in final_res.outputs:
                if request.logprobs is not None:
                    logprobs = create_logprobs(output.token_ids, output.logprobs)
                else:
                    logprobs = None
                choice_data = CompletionResponseChoice(
                    index=output.index,
                    text=output.text,
                    logprobs=logprobs,
                    finish_reason=output.finish_reason,
                )
                choices.append(choice_data)

            num_prompt_tokens = len(final_res.prompt_token_ids)
            num_generated_tokens = sum(
                len(output.token_ids) for output in final_res.outputs)
            usage = UsageInfo(
                prompt_tokens=num_prompt_tokens,
                completion_tokens=num_generated_tokens,
                total_tokens=num_prompt_tokens + num_generated_tokens,
            )
            response = CompletionResponse(
                id=request_id,
                created=created_time,
                model=model_name,
                choices=choices,
                usage=usage,
            )

            if request.stream:
                # When user requests streaming but we don't stream, we still need to
                # return a streaming response with a single event.
                response_json = response.json(ensure_ascii=False)

                async def fake_stream_generator() -> AsyncGenerator[str, None]:
                    yield f"data: {response_json}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(fake_stream_generator(),
                                        media_type="text/event-stream")

            return response


    def vllm_queue_state(self):
        """
            A sequence group represents a list of sequences associated with an input prompt.
            When VLLM receives a prompt, it generates a sequence group using beam search, which 
            produces multiple distinct output sequences instead of a greedy approach.
            This approach strikes a balance between greedy search and full dynamic programming, 
            which would be too time-consuming during inference.

            The number of unfinished sequence groups estimates the queue's workload, including swapped memory, 
            waiting queue, and preemption. This information helps with accurate auto-scaling on RunPod.
        """
        # Compute the number of running, waiting, and swapped sequences.
        num_running_seq = [len(x.seqs) for x in self.engine.engine.scheduler.running]
        num_waiting_seq = [len(x.seqs) for x in self.engine.engine.scheduler.waiting]
        num_swapped_seq = [len(x.seqs) for x in self.engine.engine.scheduler.swapped]

        return {
            'last_logging_time': self.engine.engine.scheduler.last_logging_time,
            'unfinished_sequence_groups': self.engine.engine.scheduler.get_num_unfinished_seq_groups(),
            'num_running_seq': num_running_seq,
            'num_waiting_seq': num_waiting_seq,
            'num_swapped_seq': num_swapped_seq
        }


def start_vllm_runpod(served_model: str, port: int = 443, host: str = '127.0.0.1', uvicorn_timeout: int = 5) -> RunpodVLLM:
    # Arguments for vllm engine.
    parser = argparse.ArgumentParser()
    parser = AsyncEngineArgs.add_cli_args(parser)
    args = parser.parse_args()

    # Create engine arguments from parsed arguments.
    engine_args = AsyncEngineArgs.from_cli_args(args)
    engine = AsyncLLMEngine.from_engine_args(engine_args)
    
    # Get the engine model configuration.
    engine_model_config = asyncio.run(engine.get_model_config())

    # Create a separate tokenizer to map token IDs to strings.
    tokenizer = get_tokenizer(
        engine_args.tokenizer,
        tokenizer_mode=engine_args.tokenizer_mode
    )

    # Uvicorn timeout default to 5 seconds
    uvicorn_timeout = uvicorn_timeout

    # Start the VLLM instance.
    runpod_vllm = RunpodVLLM(
        host=host,
        port=port,
        tokenizer=tokenizer,
        engine=engine,
        engine_model_config=engine_model_config,
        served_model=served_model,
        uvicorn_timeout=uvicorn_timeout,
    )
    return runpod_vllm
