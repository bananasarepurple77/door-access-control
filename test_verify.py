import boto3

rek = boto3.client("rekognition", region_name="ap-southeast-2")
P = "/home/cqlm/door/photos/"

def verify(ref, live, threshold=95):
    with open(P + ref, "rb") as a, open(P + live, "rb") as b:
        r = rek.compare_faces(
            SourceImage={"Bytes": a.read()},
            TargetImage={"Bytes": b.read()},
            SimilarityThreshold=threshold,
        )
    sims = [round(m["Similarity"], 1) for m in r["FaceMatches"]]
    print(f"{live:24} -> {'MATCH' if sims else 'NO MATCH':9} {sims}")

# verify("me_enrolled.jpg", "me_live.jpg")
# verify("me_enrolled.jpg", "someone_else.PNG")
verify("me_enrolled.jpg", "snapshot_test.jpg")
