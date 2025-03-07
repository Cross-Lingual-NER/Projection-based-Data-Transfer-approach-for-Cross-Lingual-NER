"""This module contains word aligners which are used for word2word
alignement calculation. It is neccessary part of a lot of pipelines"""

import gc
from abc import ABC, abstractmethod
from itertools import chain, product
from typing import Any, Iterable, Tuple

import awesome_align.modeling
import awesome_align.tokenization_bert
import simalign
import torch
from torch.nn.utils.rnn import pad_sequence

from src.pipelines.transforms_base import (
    PipelineTransformBase,
    batched,
    flatten_batch_dict,
)


class AlignerBase(ABC):
    def __enter__(self) -> Any:
        pass

    def __exit__(self, type, value, traceback) -> None:
        pass

    @abstractmethod
    def align(
        self, src_words: list[str], tgt_words: list[str]
    ) -> list[Tuple[int, int]]:
        pass

    def align_batched(
        self, src_words_batch: list[list[str]], tgt_words_batch: list[list[str]]
    ) -> Iterable[list[Tuple[int, int]]]:
        for src, tgt in zip(src_words_batch, tgt_words_batch):
            yield self.align(src, tgt)


class WordAlignTransform(PipelineTransformBase):
    """Performs word to word alignment calculation as a step of a pipeline.
    Take as an input derived from AlirnerBase class and use it to actual
    computation"""

    def __init__(
        self,
        word_aligner: AlignerBase,
        input_orig_words_key: str,
        input_trans_words_key: str,
        batch_size: int = 1,
    ) -> None:
        self.word_aligner = word_aligner
        self.orig_key = input_orig_words_key
        self.trans_key = input_trans_words_key
        self.batch_size = batch_size

    def __call__(self, input: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        with self.word_aligner:
            if self.batch_size > 1:
                for batch in batched(input, self.batch_size):
                    flatten_batch = flatten_batch_dict(batch)

                    orig_words_batch = []
                    trans_words_batch = []
                    empty_indices = []
                    for i, (orig_words, trans_words) in enumerate(
                        zip(flatten_batch[self.orig_key], flatten_batch[self.trans_key])
                    ):
                        if len(orig_words) == 0 or len(trans_words) == 0:
                            empty_indices.append(i)
                        else:
                            orig_words_batch.append(orig_words)
                            trans_words_batch.append(trans_words)

                    alignments = self.word_aligner.align_batched(
                        orig_words_batch, trans_words_batch
                    )

                    if len(empty_indices) > 0:
                        alignments = list(alignments)
                        for idx in empty_indices:
                            alignments.insert(idx, [])

                    for row, align in zip(batch, alignments):
                        row["word_alignments"] = align
                        yield row
            else:
                for row in input:
                    orig_words = row[self.orig_key]
                    trans_words = row[self.trans_key]

                    if len(trans_words) == 0 or len(orig_words) == 0:
                        alignments = []
                    else:
                        alignments = self.word_aligner.align(orig_words, trans_words)

                    row["word_alignments"] = alignments
                    yield row


class SimAlignAligner(AlignerBase):
    """Computes word to word alignments using SimAlign"""

    def __init__(self, **kwds) -> None:
        super().__init__()
        self.__args = kwds

    def __enter__(self) -> Any:
        self.__aligner = simalign.SentenceAligner(**self.__args)
        return self

    def __exit__(self, type, value, traceback) -> None:
        del self.__aligner
        gc.collect()
        torch.cuda.empty_cache()

    def align(
        self, src_words: list[str], tgt_words: list[str]
    ) -> list[Tuple[int, int]]:
        alignments = self.__aligner.get_word_aligns(src_words, tgt_words)
        for method in alignments:
            return alignments[method]

    def align_batched(
        self, src_words_batch: list[list[str]], tgt_words_batch: list[list[str]]
    ) -> Iterable[list[Tuple[int, int]]]:
        alignments = self.__aligner.get_word_aligns_batched(
            src_words_batch, tgt_words_batch
        )
        return map(lambda align: align[next(iter(align))], alignments)


class AwesomeAligner(AlignerBase):
    """Computes word to word alignments using AWESOME aligner"""

    def __init__(
        self,
        model_path: str,
        extraction: str = "softmax",
        softmax_threshold: float = 0.001,
        align_layer: int = 8,
        device: int = 0,
        cache_dir: str | None = None,
    ) -> None:
        super().__init__()
        self.__model_path = model_path
        self.__config = awesome_align.modeling.BertConfig.from_pretrained(
            model_path, cache_dir=cache_dir
        )
        self.__extraction = extraction
        self.__softmax_threshold = softmax_threshold
        self.__align_layer = align_layer
        self.__cache_dir = cache_dir
        self.__device = f"cuda:{device}" if device != -1 else "cpu"

    def __enter__(self) -> Any:
        self.__tokenizer = (
            awesome_align.tokenization_bert.BertTokenizer.from_pretrained(
                self.__model_path, cache_dir=self.__cache_dir
            )
        )
        self.__model = awesome_align.modeling.BertForMaskedLM.from_pretrained(
            self.__model_path,
            from_tf=bool(".ckpt" in self.__model_path),
            config=self.__config,
            cache_dir=self.__cache_dir,
        )
        self.__model.to(self.__device)
        self.__model.eval()
        return self

    def __exit__(self, type, value, traceback) -> None:
        del self.__tokenizer
        del self.__model
        gc.collect()
        torch.cuda.empty_cache()

    def align(
        self, src_words: list[str], tgt_words: list[str]
    ) -> list[Tuple[int, int]]:
        for out in self.align_batched([src_words], [tgt_words]):
            return list(out)  # return the first out

    def tokenize(self, words: list[str]) -> tuple[torch.Tensor, list[int]]:
        tokens = [self.__tokenizer.tokenize(word) for word in words]
        wid = [self.__tokenizer.convert_tokens_to_ids(x) for x in tokens]
        ids = self.__tokenizer.prepare_for_model(
            list(chain(*wid)),
            return_tensors="pt",
            max_length=self.__tokenizer.max_len,
        )["input_ids"][0]

        bpe2word_map = []
        for i, word_list in enumerate(tokens):
            bpe2word_map += [i for x in word_list]

        return ids, bpe2word_map

    def align_batched(
        self, src_words_batch: list[list[str]], tgt_words_batch: list[list[str]]
    ) -> Iterable[list[Tuple[int, int]]]:
        bpe2word_map_src, bpe2word_map_tgt = [], []
        ids_src_list, ids_tgt_list = [], []
        for src_words, tgt_words in zip(src_words_batch, tgt_words_batch):
            src_id, src_b2w = self.tokenize(src_words)
            tgt_id, tgt_b2w = self.tokenize(tgt_words)
            bpe2word_map_src.append(src_b2w)
            bpe2word_map_tgt.append(tgt_b2w)
            ids_src_list.append(src_id)
            ids_tgt_list.append(tgt_id)

        ids_src = pad_sequence(
            ids_src_list, batch_first=True, padding_value=self.__tokenizer.pad_token_id
        ).to(device=self.__device)
        ids_tgt = pad_sequence(
            ids_tgt_list, batch_first=True, padding_value=self.__tokenizer.pad_token_id
        ).to(device=self.__device)

        word_aligns = self.__model.get_aligned_word(
            ids_src,
            ids_tgt,
            bpe2word_map_src,
            bpe2word_map_tgt,
            device=self.__device,
            src_len=0,
            tgt_len=0,
            align_layer=self.__align_layer,
            extraction=self.__extraction,
            softmax_threshold=self.__softmax_threshold,
            test=True,
        )

        for align in word_aligns:
            yield list(align)


class StrictWordComparisonAligner(AlignerBase):
    """Computes word to word alignments using strict characterwise
    comparison of strings"""

    def align(
        self, src_words: list[str], tgt_words: list[str]
    ) -> list[Tuple[int, int]]:
        alignments = []
        for (i, src_word), (j, tgt_word) in product(
            enumerate(src_words), enumerate(tgt_words)
        ):
            if src_word == tgt_word:
                alignments.append((i, j))
        return alignments
