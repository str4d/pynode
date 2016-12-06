
#
# MemPool.py
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

from bitcoin.core import CheckTransaction, CheckTransactionError, b2lx


class MemPool(object):
	def __init__(self, log):
		self.pool = {}
		self.log = log

	def add(self, tx):
		hash = tx.GetHash()
		hashstr = b2lx(hash)

		if hash in self.pool:
			self.log.write("MemPool.add(%s): already known" % (hashstr,))
			return False
		try:
			CheckTransaction(tx)
		except CheckTransactionError:
			self.log.write("MemPool.add(%s): invalid TX" % (hashstr, ))
			return False

		self.pool[hash] = tx

		self.log.write("MemPool.add(%s), poolsz %d" % (hashstr, len(self.pool)))

		return True

	def remove(self, hash):
		if hash not in self.pool:
			return False

		del self.pool[hash]
		return True

	def size(self):
		return len(self.pool)


