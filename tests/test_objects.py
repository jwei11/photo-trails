from app import objects


class RecordingClient:
    def __init__(self):
        self.batches = []

    def delete_objects(self, Bucket, Delete):
        self.batches.append([o["Key"] for o in Delete["Objects"]])


def test_delete_splits_into_batches_of_1000(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(objects, "_internal", lambda: fake)

    objects.delete(*(f"k{i}" for i in range(2500)), None, "")

    assert [len(b) for b in fake.batches] == [1000, 1000, 500]
    assert sum(fake.batches, []) == [f"k{i}" for i in range(2500)]


def test_delete_with_nothing_to_delete_makes_no_request(monkeypatch):
    fake = RecordingClient()
    monkeypatch.setattr(objects, "_internal", lambda: fake)
    objects.delete()
    assert fake.batches == []
