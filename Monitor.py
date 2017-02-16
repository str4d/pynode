
#
# Monitor.py
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

import gevent
from gevent import Greenlet

from bitcoin.core import b2lx


class ForkDetector(Greenlet):
	def __init__(self, settings, log, chaindbs):
		Greenlet.__init__(self)
		self.refresh = settings['checkinterval'] if 'checkinterval' in settings else 60
		self.log = log
		self.chaindbs = chaindbs

	def _run(self):
		self.log.write('ForkDetector: Watching %d peers' % len(self.chaindbs))
		while True:
			gevent.sleep(self.refresh)
			locked = {name: cdb.acquire() for name, cdb in self.chaindbs.items()}
			self.check_chains(locked)
			[cdb.release() for name, cdb in self.chaindbs.items()]

	def check_chains(self, chaindbs):
		self.log.write('ForkDetector: Checking chains')
		tips = [((cdb.getheight(), cdb.gettophash()), name) for name, cdb in chaindbs.items()]
		tips.sort()

		# Level 1: Group common tips
		l1 = {}
		for tip in tips:
			if tip[0] in l1:
				l1[tip[0]].append(tip[1])
			else:
				l1[tip[0]] = [tip[1]]
		if len(l1) == 1:
			# All peers at same tip
			self.log.write('ForkDetector: All peers at same tip')
			return

		# Level 2: Group common chains
		l2 = []
		parent = {}
		for pt in sorted(l1, reverse=True):
			placed = False
			for tip in l2:
				cpt = tip
				# pts are reverse-sorted, so deepest parent of
				# tip will always have a height >= pt[0]
				while parent[cpt] is not None:
					cpt = parent[cpt]
				height = cpt[0]
				cur = cpt[1]
				cdb = chaindbs[l1[cpt][0]]
				while height > pt[0]:
					cur = cdb.getblock(cur).hashPrevBlock
					height -= 1
				if cur == pt[1]:
					parent[cpt] = pt
					placed = True
					break
			if not placed:
				l2.append(pt)
				parent[pt] = None
		if len(l2) == 1:
			# All peers in same chain
			self.log.write('ForkDetector: All peers in same chain')
			return

		# Level 3: Detect and report overly-large forks
		self.log.write('ForkDetector: %d independent chains detected:' % len(l2))
		for tip in l2:
			peers = l1[tip]
			cpt = tip
			while parent[cpt] is not None:
				cpt = parent[cpt]
				peers.extend(l1[cpt])
			self.log.write('- Height %d, block lx(%s): %s' % (tip[0], b2lx(tip[1]), l1[tip]))

