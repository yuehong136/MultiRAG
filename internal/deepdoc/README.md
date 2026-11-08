# Optional Go DLA client

`NewClientWithURL(baseURL).DLA(ctx, images)` sends one multipart `request` JPEG
part per image to `/predict` and reads `bboxes` rows shaped as
`[left, top, right, bottom, score, class_index]`. Results retain the input image
grouping. An empty detections array is a valid server result; network errors,
HTTP failures, malformed boxes and exhausted retries remain errors.

The client caps batches at 32 images / 32 MiB, each image at 8 MiB and each
response at 4 MiB. Every attempt has an 18 second deadline in addition to the
caller's context. It retries transient failures at most three times; redirects
and 4xx responses do not retry. Error messages do not echo server response bodies.

`NewClient` reads `DEEPDOC_URL`, then `TENSORRT_DLA_SVR`. Configuration alone does
not activate a parser. This checkout has no verified deployment DLA service;
tests cover the upstream multipart wire contract using an owned HTTP server.
No production/real-inference acceptance is implied by those tests. OCR and TSR
have no verified remote contract in this integration; they explicitly return
`ErrNoRemoteEndpoint` and do not pretend to run local ONNX inference.
