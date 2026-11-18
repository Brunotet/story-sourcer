_model = None

def _embed(texts):
    global _model
    import numpy as np
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    vecs = np.array(list(_model.embed(texts)), dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-9)
