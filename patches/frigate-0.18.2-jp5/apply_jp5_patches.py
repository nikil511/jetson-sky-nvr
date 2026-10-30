p1 = "/opt/frigate/frigate/detectors/plugins/onnx.py"
s1 = open(p1).read()
if "requires_fp16" not in s1:
    s1 = s1.replace(
        "model_type=detector_config.model.model_type,",
        "model_type=detector_config.model.model_type,\n            requires_fp16=True,",
    )
    open(p1, "w").write(s1)
    print("Patched", p1)

p2 = "/opt/frigate/frigate/util/model.py"
s2 = open(p2).read()
if "trt_max_workspace_size" not in s2:
    s2 = s2.replace(
        "\"trt_engine_cache_enable\": True,",
        "\"trt_engine_cache_enable\": True,\n                        \"trt_max_workspace_size\": 268435456,",
    )
    open(p2, "w").write(s2)
    print("Patched", p2)
