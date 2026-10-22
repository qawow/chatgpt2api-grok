"""根域名感知的域名轮换（2026-10-22）。

远端实测：所有注册账号都落在**一个根域名**下 —— `5122331.xyz` 出现 511 次、
含 **391 个不同子域**。而选域名的代码是 `random.choice(domain_pool)`：
扁平随机，没有根域名计数、没有冷却、没有拒绝记忆。

**封禁发生在根域名层面**（`account:email-domain-rejected` /
`form-invalid:disposable-email`），所以轮换子域完全无效 —— 必须按 eTLD+1
计数并轮换。
"""
import unittest

from services.gpt_register_service import (
    DomainRotator,
    looks_like_domain_rejection,
    root_domain,
)


class RootDomainTest(unittest.TestCase):
    def test_deep_subdomain_collapses_to_root(self):
        self.assertEqual(
            root_domain("rtz47.vm59hcmj.d0wv9.di2u5t.hhz5.cyt233.dpdns.org"),
            "dpdns.org")

    def test_plain_hosts(self):
        self.assertEqual(root_domain("a.b.example.com"), "example.com")
        self.assertEqual(root_domain("example.com"), "example.com")
        self.assertEqual(root_domain("localhost"), "localhost")

    def test_two_part_public_suffixes(self):
        self.assertEqual(root_domain("a.b.co.uk"), "b.co.uk")
        self.assertEqual(root_domain("x.com.cn"), "x.com.cn")
        self.assertEqual(root_domain("deep.sub.com.au"), "sub.com.au")

    def test_tolerates_garbage(self):
        for value in ("", None, "   ", "@", ".."):
            self.assertEqual(root_domain(value), "" if value in ("", None, "   ", "@", "..") else value)


class DomainRejectionTest(unittest.TestCase):
    def test_markers(self):
        for text in ("account:email-domain-rejected",
                     "form-invalid:disposable-email",
                     "unauthorized:email-domain-not-allowed",
                     "email_domain_pending"):
            self.assertTrue(looks_like_domain_rejection(text), text)

    def test_non_rejection(self):
        for text in ("", "TLS connect error", "upstream connection timed out"):
            self.assertFalse(looks_like_domain_rejection(text), text)


class DomainRotatorTest(unittest.TestCase):
    POOL = ["a.5122331.xyz", "b.5122331.xyz", "c.other.com", "d.third.net"]

    def test_it_spreads_across_roots(self):
        r = DomainRotator(max_per_root=10, cooldown_secs=60)
        picked = []
        for _ in range(12):
            d = r.pick(self.POOL)
            r.mark_used(d)
            picked.append(root_domain(d))
        self.assertEqual(set(picked), {"5122331.xyz", "other.com", "third.net"})

    def test_a_rejected_domain_cools_the_whole_root(self):
        r = DomainRotator(max_per_root=99, cooldown_secs=3600)
        r.mark_rejected("a.5122331.xyz", reason="email-domain-rejected")
        for _ in range(5):
            # 冷却的是整根，所以永远不该再选到 5122331.xyz 下的任何子域
            self.assertNotEqual(root_domain(r.pick(self.POOL)), "5122331.xyz")

    def test_saturation_still_returns_something(self):
        """全饱和时退化为「冷却最早结束」的那个，不阻断注册。"""
        r = DomainRotator(max_per_root=1, cooldown_secs=0)
        for d in self.POOL:
            r.mark_used(d)
        self.assertIn(r.pick(self.POOL), self.POOL)

    def test_an_empty_pool_raises(self):
        with self.assertRaises(ValueError):
            DomainRotator().pick([])

    def test_snapshot_roundtrip(self):
        r = DomainRotator(max_per_root=5, cooldown_secs=60)
        r.mark_used("a.5122331.xyz")
        r.mark_rejected("b.other.com", reason="x")
        snap = r.snapshot()
        r2 = DomainRotator()
        r2.load(snap)
        self.assertEqual(r2.snapshot()["used"], snap["used"])
        self.assertEqual(r2.snapshot()["cooling"], snap["cooling"])

    def test_cooldown_expires(self):
        now = [1000.0]
        r = DomainRotator(max_per_root=99, cooldown_secs=60, clock=lambda: now[0])
        r.mark_rejected("a.5122331.xyz")
        self.assertEqual(root_domain(r.pick(["a.5122331.xyz", "c.other.com"])),
                         "other.com")
        now[0] += 120
        self.assertEqual(r.pick(["a.5122331.xyz"]), "a.5122331.xyz")


if __name__ == "__main__":
    unittest.main()
