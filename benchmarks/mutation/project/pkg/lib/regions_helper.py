def check(model, tensor):
    device, dtype = _model_device_dtype(model)
    if tensor.device != device:
        raise ValueError("device mismatch")
    if tensor.dtype != dtype:
        raise ValueError("dtype mismatch")
