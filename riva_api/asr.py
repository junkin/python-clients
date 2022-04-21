import io
import os
import sys
import time
from typing import Generator, List, Optional, TextIO, Tuple, Union

import wave

import riva_api.proto.riva_asr_pb2 as rasr
import riva_api.proto.riva_asr_pb2_grpc as rasr_srv
from riva_api.auth import Auth
from riva_api.proto.riva_asr_pb2 import StreamingRecognizeResponse

ALLOWED_PREFIXES_FOR_TRANSCRIPTS = ['time', 'partial vs final', '>> vs ##']


def get_wav_file_frames_rate_duration(input_file: os.PathLike) -> Tuple[int, int, float]:
    with wave.open(str(input_file), 'rb') as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    return frames, rate, frames / rate


def audio_chunks_from_file_generator(
    input_file: os.PathLike,
    config: rasr.StreamingRecognitionConfig,
    num_iterations: int,
    simulate_realtime: bool,
    rate: int,
    file_streaming_chunk: int,
) -> Generator[rasr.StreamingRecognizeRequest, None, None]:
    try:
        for i in range(num_iterations):
            with wave.open(str(input_file), 'rb') as w:
                start_time = time.time()
                yield rasr.StreamingRecognizeRequest(streaming_config=config)
                num_requests = 0
                while True:
                    d = w.readframes(file_streaming_chunk)
                    if len(d) <= 0:
                        break
                    num_requests += 1
                    if simulate_realtime:
                        time_to_sleep = max(
                            0.0, file_streaming_chunk / rate * num_requests - (time.time() - start_time)
                        )
                        time.sleep(time_to_sleep)
                    yield rasr.StreamingRecognizeRequest(audio_content=d)
    except Exception as e:
        print(e)


def print_responses(
    generator: Generator[StreamingRecognizeResponse, None, None],
    output_file: Union[os.PathLike, TextIO],
    pretty_overwrite: bool,
    verbose: bool,
    word_time_offsets: bool,
    prefix_for_transcripts: str,
    show_intermediate: bool,
) -> None:
    if prefix_for_transcripts not in ALLOWED_PREFIXES_FOR_TRANSCRIPTS:
        raise ValueError(
            f"Wrong value '{prefix_for_transcripts}' of parameter `prefix_for_transcripts`. "
            f"Allowed values: {ALLOWED_PREFIXES_FOR_TRANSCRIPTS}."
        )
    if pretty_overwrite and prefix_for_transcripts != ALLOWED_PREFIXES_FOR_TRANSCRIPTS[2]:
        raise ValueError(
            f"If `pretty_overwrite` parameter is `True` then `prefix_for_transcripts` has to be "
            f"'{ALLOWED_PREFIXES_FOR_TRANSCRIPTS[2]}'"
        )
    if pretty_overwrite and verbose:
        raise ValueError("Parameters `pretty_overwrite` and `verbose` cannot be `True` simultaneously")
    if show_intermediate and prefix_for_transcripts != ALLOWED_PREFIXES_FOR_TRANSCRIPTS[2]:
        raise ValueError(
            f"If `show_intermediate` parameter is `True` then `prefix_for_transcripts` has to be "
            f"'{ALLOWED_PREFIXES_FOR_TRANSCRIPTS[2]}'"
        )
    num_chars_printed = 0
    if isinstance(output_file, io.TextIOWrapper):
        file_opened = False
    else:
        file_opened = True
        output_file = open(output_file, 'w')
    start_time = time.time()
    for response in generator:
        if not response.results:
            continue
        partial_transcript = ""
        for result in response.results:
            if not result.alternatives:
                continue
            if result.is_final:
                if show_intermediate:
                    transcript = result.alternatives[0].transcript
                    assert prefix_for_transcripts == ALLOWED_PREFIXES_FOR_TRANSCRIPTS[2]
                    overwrite_chars = ' ' * (num_chars_printed - len(transcript))
                    print("## " + transcript + overwrite_chars + "\n")
                    num_chars_printed = 0
                else:
                    for index, alternative in enumerate(result.alternatives):
                        if prefix_for_transcripts == ALLOWED_PREFIXES_FOR_TRANSCRIPTS[0]:
                            output_file.write(
                                f"Time {time.time() - start_time:.2f}s: Transcript {index}: {alternative.transcript}\n"
                            )
                        elif prefix_for_transcripts == ALLOWED_PREFIXES_FOR_TRANSCRIPTS[1]:
                            output_file.write(f'Final transcript: {alternative.transcript}\n')
                        else:
                            output_file.write(f'## {alternative.transcript}')

                    if word_time_offsets:
                        output_file.write("Timestamps:\n")
                        output_file.write("%-40s %-16s %-16s\n" % ("Word", "Start (ms)", "End (ms)"))
                        for word_info in result.alternatives[0].words:
                            output_file.write(
                                "%-40s %-16.0f %-16.0f\n" % (word_info.word, word_info.start_time, word_info.end_time)
                            )
            else:
                transcript = result.alternatives[0].transcript
                partial_transcript += transcript
        if prefix_for_transcripts == ALLOWED_PREFIXES_FOR_TRANSCRIPTS[0]:
            output_file.write(">>>Time %.2fs: %s\n" % (time.time() - start_time, partial_transcript))
        elif prefix_for_transcripts == ALLOWED_PREFIXES_FOR_TRANSCRIPTS[1]:
            if show_intermediate and partial_transcript != '':
                overwrite_chars = ' ' * (num_chars_printed - len(partial_transcript))
                sys.stdout.write(">> " + partial_transcript + overwrite_chars + '\r')
                sys.stdout.flush()
                num_chars_printed = len(partial_transcript) + 3
    if file_opened:
        output_file.close()


class ASR_Client:
    def __init__(self, auth: Auth) -> None:
        self.auth = auth
        self.stub = rasr_srv.RivaSpeechRecognitionStub(self.auth.channel)

    @staticmethod
    def _update_recognition_config(
        config: Union[rasr.StreamingRecognitionConfig, rasr.RecognitionConfig],
        rate: int,
        boosted_lm_words: List[str],
        boosted_lm_score: float,
    ) -> rasr.RecognitionConfig:
        inner_config: rasr.RecognitionConfig = config if isinstance(config, rasr.RecognitionConfig) else config.config
        inner_config.sample_rate_hertz = rate
        if boosted_lm_words is not None:
            speech_context = rasr.SpeechContext()
            speech_context.phrases.extend(boosted_lm_words)
            speech_context.boost = boosted_lm_score
            inner_config.speech_contexts.append(speech_context)
        return config

    def streaming_recognize_file_generator(
        self,
        input_file: os.PathLike,
        simulate_realtime: bool,
        streaming_config: rasr.StreamingRecognitionConfig,
        boosted_lm_words: Optional[List[str]] = None,
        boosted_lm_score: float = 4.0,
        num_iterations: int = 1,
        file_streaming_chunk: int = 1600,
    ) -> Generator[StreamingRecognizeResponse, None, None]:
        if boosted_lm_words is None:
            boosted_lm_words = []
        frames, rate, duration = get_wav_file_frames_rate_duration(input_file)
        self._update_recognition_config(
            config=streaming_config, rate=rate, boosted_lm_words=boosted_lm_words, boosted_lm_score=boosted_lm_score
        )
        for response in self.stub.StreamingRecognize(
            audio_chunks_from_file_generator(
                input_file, streaming_config, num_iterations, simulate_realtime, rate, file_streaming_chunk
            ),
            metadata=self.auth.get_auth_metadata(),
        ):
            yield response

    def streaming_recognize_file_print(
        self,
        input_file: os.PathLike,
        simulate_realtime: bool,
        streaming_config: rasr.StreamingRecognitionConfig,
        output_file: Union[os.PathLike, TextIO] = sys.stdout,
        boosted_lm_words: Optional[List[str]] = None,
        boosted_lm_score: float = 4.0,
        num_iterations: int = 1,
        file_streaming_chunk: int = 1600,
        prefix_for_transcripts: str = "time",
        pretty_overwrite: bool = False,
        word_time_offsets: bool = False,
        show_intermediate: bool = False,
    ) -> None:
        print_responses(
            self.streaming_recognize_file_generator(
                input_file=input_file,
                streaming_config=streaming_config,
                simulate_realtime=simulate_realtime,
                boosted_lm_words=boosted_lm_words,
                boosted_lm_score=boosted_lm_score,
                num_iterations=num_iterations,
                file_streaming_chunk=file_streaming_chunk,
            ),
            output_file,
            pretty_overwrite,
            False,
            word_time_offsets,
            prefix_for_transcripts,
            show_intermediate,
        )




