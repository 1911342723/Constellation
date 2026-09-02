"""Create no-style test documents for Constellation evaluation.

These documents have ZERO formatting cues (uniform font size, no bold,
no heading styles) but clear semantic heading structure via numbering
patterns and line breaks.  A pure rule engine cannot distinguish
headings from body text in these documents.
"""
import fitz
import json
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(PROJECT_ROOT, "tests", "data", "no_style")
os.makedirs(OUT_DIR, exist_ok=True)


def write_pdf_and_gt(name, lines, gt_headings, gt_title="Untitled"):
    """Write a PDF and its ground truth JSON."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    font = "helv"
    size = 10.0  # ALL text is the same size

    block_id = 0
    for text, _is_heading in lines:
        if y > 720:
            page = doc.new_page()
            y = 72
        page.insert_text((72, y), text, fontname=font, fontsize=size)
        y += size * 1.5
        block_id += 1

    pdf_path = os.path.join(OUT_DIR, f"{name}.pdf")
    gt_path = os.path.join(OUT_DIR, f"{name}.json")

    doc.save(pdf_path)
    doc.close()

    gt = {"doc_title": gt_title, "headings": gt_headings}
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt, f, indent=2, ensure_ascii=False)

    print(f"  {name}.pdf: {len(lines)} lines, {len(gt_headings)} headings")


def main():
    print("Creating no-style test documents...")
    print()

    # ── Doc 1: Numbered headings, uniform font ──
    lines1 = [
        ("1. Introduction", True),
        ("This is the introduction section. It provides background information about the topic and explains the motivation for the research.", False),
        ("The problem statement is clearly defined here.", False),
        ("2. Related Work", True),
        ("Previous work in this area has focused on various approaches.", False),
        ("2.1 Traditional Methods", True),
        ("Traditional methods rely on hand-crafted features and rule-based systems.", False),
        ("These methods have been shown to work well on clean data but fail on noisy inputs.", False),
        ("2.2 Deep Learning Approaches", True),
        ("Recent advances in deep learning have led to significant improvements.", False),
        ("Neural networks can learn complex patterns from data automatically.", False),
        ("3. Methodology", True),
        ("Our approach consists of three main components.", False),
        ("3.1 Data Preprocessing", True),
        ("We first clean and normalize the input data.", False),
        ("Missing values are imputed using the mean of the column.", False),
        ("3.2 Model Architecture", True),
        ("The model uses a transformer-based architecture with 12 layers.", False),
        ("Each layer has 8 attention heads and a hidden dimension of 768.", False),
        ("3.3 Training Procedure", True),
        ("We train the model using Adam optimizer with learning rate 3e-4.", False),
        ("The training runs for 100 epochs with early stopping.", False),
        ("4. Experiments", True),
        ("We evaluate our method on three benchmark datasets.", False),
        ("4.1 Dataset Description", True),
        ("Dataset A contains 10,000 samples with 50 features each.", False),
        ("Dataset B is a smaller dataset with 2,000 samples.", False),
        ("4.2 Results", True),
        ("Our method achieves state-of-the-art performance on all datasets.", False),
        ("The improvement is statistically significant with p < 0.01.", False),
        ("5. Conclusion", True),
        ("We presented a novel approach that outperforms existing methods.", False),
        ("Future work will explore extensions to multi-modal data.", False),
    ]
    gt1 = [
        {"block_id": 0, "title": "1. Introduction", "level": 1},
        {"block_id": 3, "title": "2. Related Work", "level": 1},
        {"block_id": 5, "title": "2.1 Traditional Methods", "level": 2},
        {"block_id": 8, "title": "2.2 Deep Learning Approaches", "level": 2},
        {"block_id": 11, "title": "3. Methodology", "level": 1},
        {"block_id": 13, "title": "3.1 Data Preprocessing", "level": 2},
        {"block_id": 16, "title": "3.2 Model Architecture", "level": 2},
        {"block_id": 19, "title": "3.3 Training Procedure", "level": 2},
        {"block_id": 22, "title": "4. Experiments", "level": 1},
        {"block_id": 24, "title": "4.1 Dataset Description", "level": 2},
        {"block_id": 27, "title": "4.2 Results", "level": 2},
        {"block_id": 30, "title": "5. Conclusion", "level": 1},
    ]
    write_pdf_and_gt("01_numbered_uniform_font", lines1, gt1)

    # ── Doc 2: Plain text, no numbering, no formatting ──
    lines2 = [
        ("Introduction", True),
        ("Machine learning has become an essential tool in modern data analysis. With the increasing availability of large datasets, researchers need efficient methods.", False),
        ("This paper proposes a novel approach to address these challenges.", False),
        ("Background", True),
        ("Previous approaches can be broadly categorized into two groups.", False),
        ("Supervised Methods", True),
        ("Supervised methods require labeled training data. Support vector machines and random forests are classic examples.", False),
        ("These methods achieve good performance when sufficient labeled data is available.", False),
        ("Unsupervised Methods", True),
        ("Unsupervised methods do not require labels. Clustering algorithms like k-means discover natural groupings.", False),
        ("Dimensionality reduction techniques like PCA are also commonly used.", False),
        ("Proposed Method", True),
        ("Our method combines the strengths of both supervised and unsupervised approaches.", False),
        ("Feature Extraction", True),
        ("We extract features using a pre-trained convolutional neural network.", False),
        ("The feature vectors have dimension 512 and are L2-normalized.", False),
        ("Classification", True),
        ("The extracted features are fed into a gradient boosting classifier.", False),
        ("We use 5-fold cross-validation to select hyperparameters.", False),
        ("Experiments", True),
        ("We evaluate on three benchmark datasets with different characteristics.", False),
        ("Results show that our method outperforms all baselines.", False),
        ("Conclusion", True),
        ("We presented a hybrid approach that combines supervised and unsupervised learning.", False),
        ("The method is efficient, scalable, and achieves state-of-the-art results.", False),
    ]
    gt2 = [
        {"block_id": 0, "title": "Introduction", "level": 1},
        {"block_id": 3, "title": "Background", "level": 1},
        {"block_id": 5, "title": "Supervised Methods", "level": 2},
        {"block_id": 8, "title": "Unsupervised Methods", "level": 2},
        {"block_id": 11, "title": "Proposed Method", "level": 1},
        {"block_id": 13, "title": "Feature Extraction", "level": 2},
        {"block_id": 16, "title": "Classification", "level": 2},
        {"block_id": 19, "title": "Experiments", "level": 1},
        {"block_id": 22, "title": "Conclusion", "level": 1},
    ]
    write_pdf_and_gt("02_plain_text_no_numbering", lines2, gt2)

    # ── Doc 3: Deep hierarchy (4 levels), uniform font ──
    lines3 = [
        ("1. System Overview", True),
        ("The system consists of multiple interconnected components.", False),
        ("1.1 Architecture", True),
        ("The architecture follows a microservices pattern.", False),
        ("1.1.1 Frontend", True),
        ("The frontend is built with React and TypeScript.", False),
        ("It communicates with the backend via REST API.", False),
        ("1.1.2 Backend", True),
        ("The backend is a Python FastAPI application.", False),
        ("1.1.2.1 API Layer", True),
        ("The API layer handles HTTP requests and authentication.", False),
        ("1.1.2.2 Service Layer", True),
        ("The service layer contains business logic and data access.", False),
        ("Database operations are abstracted through a repository pattern.", False),
        ("1.2 Deployment", True),
        ("The system is deployed on Kubernetes with auto-scaling.", False),
        ("2. Data Pipeline", True),
        ("Data flows through several processing stages.", False),
        ("2.1 Ingestion", True),
        ("Raw data is ingested from multiple external sources.", False),
        ("2.2 Transformation", True),
        ("Data is cleaned, normalized, and enriched with metadata.", False),
        ("2.2.1 Cleaning", True),
        ("Invalid records are filtered out using validation rules.", False),
        ("2.2.2 Normalization", True),
        ("Values are scaled to the [0, 1] range using min-max scaling.", False),
        ("2.3 Storage", True),
        ("Processed data is stored in PostgreSQL with indexing.", False),
        ("3. Results", True),
        ("The system processes 1 million records per hour.", False),
        ("3.1 Performance", True),
        ("Average latency is 50ms per request at the 95th percentile.", False),
        ("3.2 Accuracy", True),
        ("Classification accuracy is 95.3 percent on the test set.", False),
    ]
    gt3 = [
        {"block_id": 0, "title": "1. System Overview", "level": 1},
        {"block_id": 2, "title": "1.1 Architecture", "level": 2},
        {"block_id": 4, "title": "1.1.1 Frontend", "level": 3},
        {"block_id": 7, "title": "1.1.2 Backend", "level": 3},
        {"block_id": 9, "title": "1.1.2.1 API Layer", "level": 4},
        {"block_id": 11, "title": "1.1.2.2 Service Layer", "level": 4},
        {"block_id": 14, "title": "1.2 Deployment", "level": 2},
        {"block_id": 16, "title": "2. Data Pipeline", "level": 1},
        {"block_id": 18, "title": "2.1 Ingestion", "level": 2},
        {"block_id": 20, "title": "2.2 Transformation", "level": 2},
        {"block_id": 22, "title": "2.2.1 Cleaning", "level": 3},
        {"block_id": 24, "title": "2.2.2 Normalization", "level": 3},
        {"block_id": 27, "title": "2.3 Storage", "level": 2},
        {"block_id": 29, "title": "3. Results", "level": 1},
        {"block_id": 31, "title": "3.1 Performance", "level": 2},
        {"block_id": 33, "title": "3.2 Accuracy", "level": 2},
    ]
    write_pdf_and_gt("03_deep_hierarchy_uniform", lines3, gt3)

    # ── Doc 4: Mixed language (Chinese headings), uniform font ──
    lines4 = [
        ("Introduction", True),
        ("Document structure extraction is an important task in NLP.", False),
        ("It enables downstream applications like RAG and information retrieval.", False),
        ("Background", True),
        ("Existing methods can be categorized into rule-based and learning-based.", False),
        ("Rule-Based Methods", True),
        ("Rule-based methods use font size, bold, and numbering patterns.", False),
        ("They work well on well-formatted documents but fail on plain text.", False),
        ("Learning-Based Methods", True),
        ("Learning-based methods use neural networks to predict heading boundaries.", False),
        ("They require large amounts of labeled training data.", False),
        ("Our Approach", True),
        ("We propose a hybrid method that uses LLM for semantic understanding.", False),
        ("Skeleton Compression", True),
        ("The document is compressed to 10-30 percent of its original size.", False),
        ("This reduces LLM API costs significantly.", False),
        ("Cursor Routing", True),
        ("The LLM annotates section boundaries on the compressed skeleton.", False),
        ("It outputs block_id, level, and title for each heading.", False),
        ("Deterministic Assembly", True),
        ("A deterministic algorithm reconstructs the full document tree.", False),
        ("This guarantees zero content loss.", False),
        ("Evaluation", True),
        ("We test on 12 academic papers and 9 benchmark documents.", False),
        ("Our method achieves 2.6x higher F1 than pure LLM E2E.", False),
        ("Conclusion", True),
        ("Control-dataflow decoupling is the key to reliable document parsing.", False),
    ]
    gt4 = [
        {"block_id": 0, "title": "Introduction", "level": 1},
        {"block_id": 3, "title": "Background", "level": 1},
        {"block_id": 5, "title": "Rule-Based Methods", "level": 2},
        {"block_id": 8, "title": "Learning-Based Methods", "level": 2},
        {"block_id": 11, "title": "Our Approach", "level": 1},
        {"block_id": 13, "title": "Skeleton Compression", "level": 2},
        {"block_id": 16, "title": "Cursor Routing", "level": 2},
        {"block_id": 19, "title": "Deterministic Assembly", "level": 2},
        {"block_id": 22, "title": "Evaluation", "level": 1},
        {"block_id": 25, "title": "Conclusion", "level": 1},
    ]
    write_pdf_and_gt("04_mixed_language_uniform", lines4, gt4)

    print()
    print(f"All documents saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
