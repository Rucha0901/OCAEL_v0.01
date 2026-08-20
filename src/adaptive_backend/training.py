from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_TOP_LEVEL = {
    "domain",
    "action",
    "target_concept",
    "evidence",
    "learner_state",
    "target_response",
}


def validate_record(record: dict[str, Any], line_number: int) -> None:
    missing = REQUIRED_TOP_LEVEL - set(record)
    if missing:
        raise ValueError(f"line {line_number}: missing fields {sorted(missing)}")
    if not isinstance(record["target_response"], dict):
        raise ValueError(f"line {line_number}: target_response must be an object")
    message = record["target_response"].get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError(f"line {line_number}: target_response.message is required")


def load_records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"line {line_no}: expected JSON object")
            validate_record(record, line_no)
            rows.append(record)
    if not rows:
        raise ValueError("training dataset is empty")
    return rows


def to_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You are a concise adaptive tutor. Use the learner evidence and state. "
        "Do not invent history. Follow the requested pedagogical action and do not "
        "leak a full solution when a hint is requested."
    )
    user_payload = {
        "domain": record["domain"],
        "action": record["action"],
        "target_concept": record["target_concept"],
        "evidence": record["evidence"],
        "learner_state": record["learner_state"],
        "retrieved_context": record.get("retrieved_context", []),
        "constraints": record.get("constraints", {}),
    }
    assistant = record["target_response"]["message"]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        {"role": "assistant", "content": assistant},
    ]


def train_qlora(
    *,
    model_name: str,
    train_path: Path,
    output_dir: Path,
    max_length: int = 1536,
    epochs: float = 2.0,
    learning_rate: float = 2e-4,
    lora_rank: int = 16,
) -> None:
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Training dependencies are not installed. Install with: pip install -e '.[train]'"
        ) from exc

    records = load_records(train_path)
    tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=quant,
        device_map="auto",
        torch_dtype="auto",
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    lora = LoraConfig(
        r=lora_rank,
        lora_alpha=lora_rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    from peft import get_peft_model

    model = get_peft_model(model, lora)

    def encode(example: dict[str, Any]) -> dict[str, Any]:
        messages = to_messages(example)
        prompt_text = tokenizer.apply_chat_template(
            messages[:-1], tokenize=False, add_generation_prompt=True
        )
        full_text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
        encoded = tokenizer(
            full_text,
            truncation=True,
            max_length=max_length,
            padding=False,
        )
        prompt_ids = tokenizer(
            prompt_text,
            truncation=True,
            max_length=max_length,
            padding=False,
            add_special_tokens=False,
        )["input_ids"]
        labels = list(encoded["input_ids"])
        prompt_len = min(len(prompt_ids), len(labels))
        labels[:prompt_len] = [-100] * prompt_len
        if all(label == -100 for label in labels):
            raise ValueError(
                "max_length truncated the assistant target completely; increase max_length or shorten the record"
            )
        encoded["labels"] = labels
        return encoded

    dataset = Dataset.from_list(records)
    dataset = dataset.map(encode, remove_columns=dataset.column_names)

    output_dir.mkdir(parents=True, exist_ok=True)
    args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=learning_rate,
        logging_steps=5,
        save_strategy="epoch",
        report_to=[],
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        remove_unused_columns=False,
    )

    def collate(batch: list[dict[str, Any]]) -> dict[str, Any]:
        max_len = max(len(x["input_ids"]) for x in batch)
        pad = tokenizer.pad_token_id
        input_ids, attention, labels = [], [], []
        for item in batch:
            n = len(item["input_ids"])
            padding = max_len - n
            input_ids.append(item["input_ids"] + [pad] * padding)
            attention.append(item["attention_mask"] + [0] * padding)
            labels.append(item["labels"] + [-100] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }

    trainer = Trainer(model=model, args=args, train_dataset=dataset, data_collator=collate)
    trainer.train()
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate or QLoRA-train the pedagogical adapter")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--model", required=False, help="Hugging Face base model ID; required unless --validate-only")
    parser.add_argument("--output", type=Path, default=Path("./artifacts/pedagogy-lora"))
    parser.add_argument("--max-length", type=int, default=1536)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--lora-rank", type=int, default=16)
    args = parser.parse_args()

    rows = load_records(args.dataset)
    print(f"validated {len(rows)} records")
    if args.validate_only:
        return
    if not args.model:
        parser.error("--model is required for training")
    train_qlora(
        model_name=args.model,
        train_path=args.dataset,
        output_dir=args.output,
        max_length=args.max_length,
        epochs=args.epochs,
        learning_rate=args.lr,
        lora_rank=args.lora_rank,
    )


if __name__ == "__main__":
    main()
