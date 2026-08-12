def run(model, tensor, scale):
    check(model, tensor)
    model.eval()
    device, dtype = _model_device_dtype(model)
    if tensor.device != device:
        raise ValueError("device mismatch")
    if tensor.dtype != dtype:
        raise ValueError("dtype mismatch")
    if scale <= 0:
        raise ValueError("scale must be positive")
    if tuple(tensor.shape) != (2, 2):
        raise ValueError("unexpected shape")
    return tensor.mul(scale)
