import logging
import torch

from transformers.file_utils import ModelOutput
from transformers.generation_stopping_criteria import validate_stopping_criteria
from rouge_score import rouge_scorer

from intunlu.finetunning import SummarizerModel
from intunlu.utils import load_data


class EnsembleGenerator:

    def __init__(self, paths):
        self.models = []
        for path in paths:
            print(f'Loading {path}')
            self.models.append(SummarizerModel.load_from_checkpoint(path))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        for model in self.models:
            model.model.to(self.device)


    def generate_greedy_search(self, src_text, pool_type='mean'):

        # Check if all model are of the same type
        #TODO: might want to implement more checks in case we extend this to other models / experiments
        ref_type = type(self.models[0])
        for model in self.models[1:]:
            if not(isinstance(model, ref_type)):
                raise ValueError('Not all models are of the same type.')
        ref_type = type(self.models[0].tokenizer)
        for model in self.models[1:]:
            if not(isinstance(model.tokenizer, ref_type)):
                raise ValueError('Not all tokenizer are of the same type.')

        # get variables needed for generation. Use first model
        max_length = self.models[0].model.config.max_length
        pad_token_id = self.models[0].model.config.pad_token_id
        eos_token_id = self.models[0].model.config.eos_token_id
        bos_token_id = self.models[0].model.config.bos_token_id
        num_beam_groups = self.models[0].model.config.num_beam_groups
        decoder_start_token_id = self.models[0].tokenizer.pad_token_id
        num_beams=1

        # Tokenize input text
        src = self.models[0].tokenizer(
            'summarize: ' + src_text,
            padding='longest',
            truncation=True,
            return_tensors='pt'
        )

        input_ids = src['input_ids'].to(self.device)
        encoder_input_ids = input_ids if self.models[0].model.config.is_encoder_decoder else None

        model_kwargs= {
            'attention_mask': src['attention_mask'].to(self.device)
        }

        # special case if pad_token_id is not defined
        if pad_token_id is None and eos_token_id is not None:
            print(f"Setting `pad_token_id` to `eos_token_id`:{eos_token_id} for open-end generation.")
            pad_token_id = eos_token_id

        if self.models[0].model.config.is_encoder_decoder:
            # add encoder_outputs to model_kwargs
            model_kwargs = self.models[0].model._prepare_encoder_decoder_kwargs_for_generation(input_ids, model_kwargs)

            # set input_ids as decoder_input_ids
            if "decoder_input_ids" in model_kwargs:
                input_ids = model_kwargs.pop("decoder_input_ids")
            else:
                input_ids = self.models[0].model._prepare_decoder_input_ids_for_generation(
                    input_ids, decoder_start_token_id=decoder_start_token_id, bos_token_id=bos_token_id
                )

            if "encoder_outputs" not in model_kwargs or not isinstance(model_kwargs["encoder_outputs"], ModelOutput):
                raise ValueError("Make sure that `model_kwargs` include `encoder_outputs` of type `ModelOutput`.")

        if input_ids.shape[-1] >= max_length:
            input_ids_string = "decoder_input_ids" if self.models[0].model.config.is_encoder_decoder else "input_ids"
            print(
                f"Input length of {input_ids_string} is {input_ids.shape[-1]}, but ``max_length`` is set to {max_length}."
                "This can lead to unexpected behavior. You should consider increasing ``config.max_length`` or ``max_length``."
            )

        logits_processor = self.models[0].model._get_logits_processor(
            repetition_penalty=None,
            no_repeat_ngram_size=None,
            encoder_no_repeat_ngram_size=None,
            encoder_input_ids=encoder_input_ids,
            bad_words_ids=None,
            min_length=None,
            max_length=max_length,
            eos_token_id=eos_token_id,
            forced_bos_token_id=None,
            forced_eos_token_id=None,
            prefix_allowed_tokens_fn=None,
            num_beams=num_beams,
            num_beam_groups=num_beam_groups,
            diversity_penalty=None
        )

        stopping_criteria = self.models[0].model._get_stopping_criteria(
            max_length=max_length,
            max_time=None,
        )  
        validate_stopping_criteria(stopping_criteria, max_length)

        sequence_lengths, unfinished_sequences, cur_len = self.models[0].model._init_sequence_length_for_generation(
            input_ids, max_length
        )

        model_kwargs ['use_cache'] = True

        with torch.no_grad():
    
            while cur_len < max_length:

                model_inputs = self.models[0].model.prepare_inputs_for_generation(input_ids, **model_kwargs)

                next_token_logits = []
                for model in self.models:

                    outputs = model.model(
                        **model_inputs,
                        return_dict=True
                    )

                    next_token_logits.append(outputs.logits[:, -1, :])
                next_token_logits = torch.cat(next_token_logits, dim=0)

                if pool_type == 'mean':
                    next_token_logits = torch.mean(next_token_logits, dim=0, keepdim=True)
                elif pool_type == 'max':
                    next_token_logits = torch.max(next_token_logits, dim=0, keepdim=True)[0]
                else:
                    raise ValueError(f'`pool_type={pool_type}` is not a recognized value.')

                next_tokens_scores = logits_processor(input_ids, next_token_logits)
                next_tokens = torch.argmax(next_tokens_scores, dim=-1)

                if eos_token_id is not None:
                    assert pad_token_id is not None, "If eos_token_id is defined, make sure that pad_token_id is defined."
                    next_tokens = next_tokens * unfinished_sequences + (pad_token_id) * (1 - unfinished_sequences)

                # add token and increase length by one
                input_ids = torch.cat([input_ids, next_tokens[:, None]], dim=-1)

                    # update sequence length
                if eos_token_id is not None:
                    sequence_lengths, unfinished_sequences = self.models[0].model._update_seq_length_for_generation(
                        sequence_lengths, unfinished_sequences, cur_len, next_tokens == eos_token_id
                    )

                # update model kwargs
                model_kwargs = self.models[0].model._update_model_kwargs_for_generation(
                    outputs, model_kwargs, is_encoder_decoder=model.model.config.is_encoder_decoder
                )

                # stop when there is a </s> in each sentence, or if we exceed the maximum length
                if unfinished_sequences.max() == 0:
                    break

                if stopping_criteria(input_ids, None):
                    break

                # increase cur_len
                cur_len = cur_len + 1

            pred = self.models[0].tokenizer.convert_ids_to_tokens(input_ids[0], skip_special_tokens=True)
            pred = self.models[0].tokenizer.convert_tokens_to_string(pred).replace(' . ', '. ')

        return pred


if __name__ == '__main__':
    load_model()
