import unittest
from unittest.mock import Mock
from lanpilot.tunnel_supervisor import supervise


class Stop:
 def __init__(self,steps):self.steps=iter(steps);self.stopped=False
 def is_set(self):return self.stopped
 def wait(self,seconds):
  self.stopped=next(self.steps,True)
  return self.stopped


class TunnelSupervisorTests(unittest.TestCase):
 def test_three_failed_pinned_probes_restart_only_transport(self):
  first=Mock();second=Mock();first.poll.return_value=None;second.poll.return_value=None
  launch=Mock(side_effect=[first,second]);probe=Mock(return_value=False)
  # Three unhealthy intervals, retry delay, then shutdown the replacement.
  stop=Stop([False,False,False,False,True])
  command=['bore','local','10000','--to','relay','--port','20000']
  supervise(command,'relay',20000,'a'*64,stop,launch,probe)
  self.assertEqual(launch.call_count,2);self.assertEqual(probe.call_count,3)
  first.terminate.assert_called_once();second.terminate.assert_called_once()
  self.assertEqual(launch.call_args.args[0],command)
 def test_healthy_route_does_not_restart(self):
  child=Mock();child.poll.return_value=None;launch=Mock(return_value=child)
  probe=Mock(return_value=True)
  supervise(['bore'],'relay',20000,'a'*64,Stop([False,False,False,True]),launch,probe)
  self.assertEqual(launch.call_count,1);self.assertEqual(probe.call_count,3)
